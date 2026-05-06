"""Catalyst agent — Claude tool-use loop per spec §5.4.

Operates as a closed-loop grounded agent: tools return primary-source dates,
the model assembles them, the post-processor drops anything past `today` and
anything not seen from a tool result. Validation failures get one retry; on
the second failure the agent returns `{"no_known_catalysts": true}` and the
fact is logged to `health_log`.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, HttpUrl
from pydantic import ValidationError as PydanticValidationError

from src.adapters import token_unlocks, trading_economics
from src.catalyst.prompt import build_system_prompt
from src.catalyst.tools import TOOL_DEFINITIONS
from src.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, KL_TZ
from src.db import health
from src.validation.checks import is_future

logger = logging.getLogger(__name__)


ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class CatalystEvent(BaseModel):
    event_type: str
    event_date: date
    description: str = ""
    source_url: HttpUrl
    source_pulled_at: datetime


class NewsTheme(BaseModel):
    summary: str
    date: date
    source_url: HttpUrl
    stale: bool = False


class CatalystOutput(BaseModel):
    ticker: str
    as_of: datetime
    no_known_catalysts: bool = False
    confirmed: list[CatalystEvent] = Field(default_factory=list)
    expected: list[CatalystEvent] = Field(default_factory=list)
    speculative: list[CatalystEvent] = Field(default_factory=list)
    news_themes: list[NewsTheme] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


def _default_dispatcher(db_path: Path) -> ToolDispatcher:
    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "get_token_unlocks":
                return await token_unlocks.get_token_unlocks(db_path, **args)
            if name == "get_macro_events":
                return await trading_economics.get_macro_events(db_path, **args)
        except Exception as exc:
            logger.exception("Tool dispatch %s raised", name)
            return {"items": [], "error": str(exc)}
        return {"items": [], "error": f"unknown tool: {name}"}

    return dispatch


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _harvest_dates(payload: dict[str, Any]) -> set[date]:
    """Pull every plausible date out of a tool-result payload."""
    seen: set[date] = set()
    for item in payload.get("items", []) or []:
        for key in ("event_date", "unlock_date", "date", "earnings_date", "filing_date"):
            v = item.get(key) if isinstance(item, dict) else None
            if not isinstance(v, str):
                continue
            try:
                seen.add(date.fromisoformat(v[:10]))
            except ValueError:
                continue
    return seen


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text


def _drop_ungrounded(output: CatalystOutput, seen: set[date], today: date) -> CatalystOutput:
    """Per spec §7 rule 1, every date must trace back to a tool result.

    Combined with `is_future` to enforce spec §5.4 rule 3 (no past dates).
    Mutates `output` in place and returns it for chaining.
    """

    def keep_event(e: CatalystEvent) -> bool:
        return e.event_date in seen and is_future(e.event_date, today=today)

    def keep_news(n: NewsTheme) -> bool:
        # News dates are 0–7 days back per spec §5.4 rule 7; we don't enforce
        # the future filter here, only the grounding filter.
        return n.date in seen

    output.confirmed = [e for e in output.confirmed if keep_event(e)]
    output.expected = [e for e in output.expected if keep_event(e)]
    output.speculative = [e for e in output.speculative if keep_event(e)]
    output.news_themes = [n for n in output.news_themes if keep_news(n)]
    return output


async def run_catalyst_agent(
    ticker: str,
    asset_class: str,
    db_path: Path,
    *,
    anthropic_client: AsyncAnthropic | None = None,
    tool_dispatcher: ToolDispatcher | None = None,
    max_turns: int = 8,
) -> CatalystOutput:
    """Run one catalyst-agent invocation; return the validated, grounded output."""
    client = anthropic_client or AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    dispatcher = tool_dispatcher or _default_dispatcher(db_path)

    now_kl = datetime.now(KL_TZ)
    today_kl = now_kl.date()
    system_prompt = build_system_prompt(
        today_iso=today_kl.isoformat(),
        now_iso=now_kl.replace(microsecond=0).isoformat(),
    )

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Generate catalyst data for ticker {ticker.upper()} "
                f"(asset_class={asset_class}). Use tools per the asset-class rules "
                f"and return JSON matching the CatalystOutput schema."
            ),
        }
    ]

    seen_dates: set[date] = set()
    retry_used = False

    for turn in range(max_turns):
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        tool_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if response.stop_reason == "tool_use" and tool_blocks:
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tb in tool_blocks:
                args = dict(tb.input or {})
                try:
                    payload = await dispatcher(tb.name, args)
                except Exception as exc:
                    logger.exception("Tool %s failed", tb.name)
                    payload = {"items": [], "error": str(exc)}
                seen_dates |= _harvest_dates(payload)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tb.id,
                        "content": json.dumps(payload, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
        )
        text = _strip_code_fences(text)

        try:
            data = json.loads(text)
            output = CatalystOutput.model_validate(data)
        except (json.JSONDecodeError, PydanticValidationError) as exc:
            err = str(exc)[:500]
            logger.warning("Catalyst output failed validation (turn %d): %s", turn, err)
            if retry_used:
                await health.record(
                    db_path,
                    source="catalyst-agent",
                    status="error",
                    details=f"{ticker}: validation failed twice — {err}",
                )
                return CatalystOutput(
                    ticker=ticker.upper(),
                    as_of=now_kl,
                    no_known_catalysts=True,
                    flags=["agent validation failed"],
                )
            retry_used = True
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your previous response did not parse as the CatalystOutput "
                        f"schema. Error: {err}. Return ONLY corrected JSON — no prose, "
                        f"no preamble, no code fences."
                    ),
                }
            )
            continue

        return _drop_ungrounded(output, seen_dates, today_kl)

    logger.error("Catalyst agent hit max_turns=%d for %s", max_turns, ticker)
    return CatalystOutput(
        ticker=ticker.upper(),
        as_of=now_kl,
        no_known_catalysts=True,
        flags=[f"agent hit max_turns ({max_turns})"],
    )
