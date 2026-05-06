"""Phase 5 def-of-done: agent never produces a date not seen from a tool.

These tests mock the anthropic client and the tool dispatcher so the agent
loop is exercised end-to-end without network or API keys.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.catalyst import agent as agent_mod
from src.catalyst.agent import (
    CatalystEvent,
    CatalystOutput,
    NewsTheme,
    _drop_ungrounded,
    _harvest_dates,
    _strip_code_fences,
    run_catalyst_agent,
)
from src.db.schema import init_db

# ---------------------------------------------------------------------------
# Fake anthropic response helpers
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content: list, stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


def _tool_use_response(name: str, args: dict, tool_id: str = "t1") -> _Response:
    return _Response(
        content=[_Block("tool_use", name=name, input=args, id=tool_id)],
        stop_reason="tool_use",
    )


def _text_response(text: str) -> _Response:
    return _Response(
        content=[_Block("text", text=text)],
        stop_reason="end_turn",
    )


class _FakeAnthropic:
    """Minimal stand-in for AsyncAnthropic.messages.create()."""

    def __init__(self, scripted: list[_Response]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict] = []
        self.messages = self  # so client.messages.create works

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if not self._scripted:
            raise RuntimeError("No more scripted responses")
        return self._scripted.pop(0)


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "agent_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_strip_code_fences_plain() -> None:
    assert _strip_code_fences('{"a": 1}') == '{"a": 1}'


def test_strip_code_fences_with_json_lang() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert _strip_code_fences(raw) == '{"a": 1}'


def test_strip_code_fences_unlabeled() -> None:
    raw = '```\n{"a": 1}\n```'
    assert _strip_code_fences(raw) == '{"a": 1}'


def test_harvest_dates_picks_event_and_unlock_date() -> None:
    payload = {
        "items": [
            {"event_date": "2026-05-15"},
            {"unlock_date": "2026-06-01"},
            {"date": "2026-05-20"},
            {"event_date": "not-a-date"},
            {"event_date": None},
            "junk-not-a-dict",
        ]
    }
    seen = _harvest_dates(payload)
    assert date(2026, 5, 15) in seen
    assert date(2026, 6, 1) in seen
    assert date(2026, 5, 20) in seen
    assert len(seen) == 3


def test_harvest_dates_handles_missing_items() -> None:
    assert _harvest_dates({}) == set()
    assert _harvest_dates({"items": None}) == set()


# ---------------------------------------------------------------------------
# _drop_ungrounded: the grounding gate
# ---------------------------------------------------------------------------


def _ev(d: date, source: str = "https://x.example.com/") -> CatalystEvent:
    return CatalystEvent(
        event_type="test",
        event_date=d,
        description="x",
        source_url=source,
        source_pulled_at=datetime.now(UTC),
    )


def _news(d: date) -> NewsTheme:
    return NewsTheme(summary="s", date=d, source_url="https://news.example.com/")


def test_drop_ungrounded_removes_unseen_dates() -> None:
    today = date(2026, 5, 6)
    seen = {date(2026, 5, 15), date(2026, 5, 20)}
    out = CatalystOutput(
        ticker="SOL",
        as_of=datetime.now(UTC),
        confirmed=[_ev(date(2026, 5, 15)), _ev(date(2026, 5, 30))],  # second is fabricated
        expected=[_ev(date(2026, 5, 20))],
        speculative=[_ev(date(2027, 1, 1))],  # fabricated
        news_themes=[_news(date(2026, 5, 15)), _news(date(2026, 5, 1))],  # second unseen
    )
    out = _drop_ungrounded(out, seen, today)
    assert [e.event_date for e in out.confirmed] == [date(2026, 5, 15)]
    assert [e.event_date for e in out.expected] == [date(2026, 5, 20)]
    assert out.speculative == []
    assert [n.date for n in out.news_themes] == [date(2026, 5, 15)]


def test_drop_ungrounded_filters_past_event_dates() -> None:
    today = date(2026, 5, 6)
    seen = {date(2026, 5, 1), date(2026, 5, 10)}
    out = CatalystOutput(
        ticker="SOL",
        as_of=datetime.now(UTC),
        confirmed=[_ev(date(2026, 5, 1)), _ev(date(2026, 5, 10))],
    )
    out = _drop_ungrounded(out, seen, today)
    assert [e.event_date for e in out.confirmed] == [date(2026, 5, 10)]


# ---------------------------------------------------------------------------
# End-to-end agent loop
# ---------------------------------------------------------------------------


def _final_json(ticker: str, dates: list[date]) -> str:
    return json.dumps(
        {
            "ticker": ticker,
            "as_of": datetime.now(UTC).isoformat(),
            "no_known_catalysts": False,
            "confirmed": [
                {
                    "event_type": "token_unlock",
                    "event_date": d.isoformat(),
                    "description": f"{ticker} unlock",
                    "source_url": "https://token.unlocks.app",
                    "source_pulled_at": datetime.now(UTC).isoformat(),
                }
                for d in dates
            ],
            "expected": [],
            "speculative": [],
            "news_themes": [],
            "flags": [],
        }
    )


@pytest.mark.asyncio
async def test_agent_returns_only_dates_seen_from_tools(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the agent obeys grounding, every reported date came from a tool."""
    monkeypatch.setattr(agent_mod, "ANTHROPIC_MODEL", "fake-model-id")

    today = datetime.now(agent_mod.KL_TZ).date()
    unlock_date = today + timedelta(days=10)

    fake_dispatcher = AsyncMock()
    fake_dispatcher.return_value = {
        "items": [
            {
                "symbol": "SOL",
                "unlock_date": unlock_date.isoformat(),
                "description": "SOL unlock",
                "source_url": "https://token.unlocks.app",
            }
        ],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://token.unlocks.app",
    }

    fake = _FakeAnthropic(
        [
            _tool_use_response("get_token_unlocks", {"symbol": "SOL", "days_ahead": 30}),
            _text_response(_final_json("SOL", [unlock_date])),
        ]
    )

    out = await run_catalyst_agent(
        "SOL", "crypto", db_path, anthropic_client=fake, tool_dispatcher=fake_dispatcher
    )

    assert out.ticker == "SOL"
    assert [e.event_date for e in out.confirmed] == [unlock_date]
    assert out.no_known_catalysts is False
    fake_dispatcher.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_strips_fabricated_dates(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the model emits a date that wasn't returned by any tool, the
    grounding filter must drop it."""
    monkeypatch.setattr(agent_mod, "ANTHROPIC_MODEL", "fake-model-id")

    today = datetime.now(agent_mod.KL_TZ).date()
    real_date = today + timedelta(days=10)
    fake_date = today + timedelta(days=42)  # never returned by tools

    fake_dispatcher = AsyncMock()
    fake_dispatcher.return_value = {
        "items": [{"symbol": "SOL", "unlock_date": real_date.isoformat()}],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://token.unlocks.app",
    }

    fake = _FakeAnthropic(
        [
            _tool_use_response("get_token_unlocks", {"symbol": "SOL"}),
            _text_response(_final_json("SOL", [real_date, fake_date])),
        ]
    )

    out = await run_catalyst_agent(
        "SOL", "crypto", db_path, anthropic_client=fake, tool_dispatcher=fake_dispatcher
    )

    dates_out = [e.event_date for e in out.confirmed]
    assert real_date in dates_out
    assert fake_date not in dates_out, "fabricated date should be dropped"


@pytest.mark.asyncio
async def test_agent_retries_on_validation_failure(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First response is unparseable JSON → agent retries → second is valid."""
    monkeypatch.setattr(agent_mod, "ANTHROPIC_MODEL", "fake-model-id")

    today = datetime.now(agent_mod.KL_TZ).date()
    d = today + timedelta(days=5)

    fake_dispatcher = AsyncMock()
    fake_dispatcher.return_value = {
        "items": [{"unlock_date": d.isoformat()}],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://token.unlocks.app",
    }

    fake = _FakeAnthropic(
        [
            _tool_use_response("get_token_unlocks", {"symbol": "SOL"}),
            _text_response("{not valid json"),
            _text_response(_final_json("SOL", [d])),
        ]
    )

    out = await run_catalyst_agent(
        "SOL", "crypto", db_path, anthropic_client=fake, tool_dispatcher=fake_dispatcher
    )

    assert out.no_known_catalysts is False
    assert [e.event_date for e in out.confirmed] == [d]


@pytest.mark.asyncio
async def test_agent_double_validation_failure_returns_no_catalysts(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive bad outputs → safe `no_known_catalysts` fallback."""
    monkeypatch.setattr(agent_mod, "ANTHROPIC_MODEL", "fake-model-id")

    fake_dispatcher = AsyncMock()
    fake_dispatcher.return_value = {
        "items": [],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://token.unlocks.app",
    }

    fake = _FakeAnthropic(
        [
            _tool_use_response("get_token_unlocks", {"symbol": "SOL"}),
            _text_response("garbage 1"),
            _text_response("still garbage"),
        ]
    )

    out = await run_catalyst_agent(
        "SOL", "crypto", db_path, anthropic_client=fake, tool_dispatcher=fake_dispatcher
    )

    assert out.no_known_catalysts is True
    assert any("validation failed" in f for f in out.flags)


@pytest.mark.asyncio
async def test_agent_no_known_catalysts_passthrough(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model legitimately returns no_known_catalysts when tools find nothing."""
    monkeypatch.setattr(agent_mod, "ANTHROPIC_MODEL", "fake-model-id")

    fake_dispatcher = AsyncMock()
    fake_dispatcher.return_value = {
        "items": [],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://token.unlocks.app",
    }

    final = json.dumps(
        {
            "ticker": "OBSCURE",
            "as_of": datetime.now(UTC).isoformat(),
            "no_known_catalysts": True,
        }
    )
    fake = _FakeAnthropic(
        [
            _tool_use_response("get_token_unlocks", {"symbol": "OBSCURE"}),
            _text_response(final),
        ]
    )

    out = await run_catalyst_agent(
        "OBSCURE", "crypto", db_path, anthropic_client=fake, tool_dispatcher=fake_dispatcher
    )
    assert out.no_known_catalysts is True
    assert out.confirmed == []


# ---------------------------------------------------------------------------
# Default tool dispatcher routes correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_dispatcher_routes_all_tools(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, dict] = {}

    async def _fake(name):
        async def inner(*args, **kwargs):
            calls[name] = kwargs
            return {"items": [], "pulled_at": "", "source_url": ""}

        return inner

    monkeypatch.setattr(
        "src.catalyst.agent.token_unlocks.get_token_unlocks", await _fake("token_unlocks")
    )
    monkeypatch.setattr(
        "src.catalyst.agent.trading_economics.get_macro_events", await _fake("macro")
    )
    monkeypatch.setattr("src.catalyst.agent.finnhub.get_earnings_calendar", await _fake("earnings"))
    monkeypatch.setattr("src.catalyst.agent.finnhub.search_news", await _fake("news"))
    monkeypatch.setattr("src.catalyst.agent.sec_edgar.read_sec_filings", await _fake("sec"))

    dispatcher = agent_mod._default_dispatcher(db_path)

    await dispatcher("get_token_unlocks", {"symbol": "SOL", "days_ahead": 7})
    await dispatcher("get_macro_events", {"days_ahead": 14, "importance_min": 2})
    await dispatcher("get_earnings_calendar", {"ticker": "NVDA", "days_ahead": 60})
    await dispatcher("search_news", {"query": "NVDA", "days_back": 7})
    await dispatcher("read_sec_filings", {"ticker": "NVDA", "form_types": ["8-K"], "days_back": 30})
    unknown = await dispatcher("nope", {})

    assert calls["token_unlocks"] == {"symbol": "SOL", "days_ahead": 7}
    assert calls["macro"] == {"days_ahead": 14, "importance_min": 2}
    assert calls["earnings"] == {"ticker": "NVDA", "days_ahead": 60}
    assert calls["news"] == {"query": "NVDA", "days_back": 7}
    assert calls["sec"] == {"ticker": "NVDA", "form_types": ["8-K"], "days_back": 30}
    assert unknown["items"] == []
    assert "unknown tool" in unknown["error"]


@pytest.mark.asyncio
async def test_agent_equity_path_with_earnings_and_filings(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end equity flow: earnings (future) + 8-K (past, news) + news themes."""
    monkeypatch.setattr(agent_mod, "ANTHROPIC_MODEL", "fake-model-id")

    today = datetime.now(agent_mod.KL_TZ).date()
    earnings_date = today + timedelta(days=14)
    filing_date = today - timedelta(days=2)
    news_date = today - timedelta(days=1)

    earnings_payload = {
        "items": [
            {
                "ticker": "NVDA",
                "earnings_date": earnings_date.isoformat(),
                "event_date": earnings_date.isoformat(),
                "hour": "amc",
                "source_url": "https://finnhub.io",
            }
        ],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://finnhub.io",
    }
    sec_payload = {
        "items": [
            {
                "ticker": "NVDA",
                "form": "8-K",
                "filing_date": filing_date.isoformat(),
                "event_date": filing_date.isoformat(),
                "source_url": "https://www.sec.gov/Archives/edgar/data/1045810/x.htm",
            }
        ],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://www.sec.gov",
    }
    news_payload = {
        "items": [
            {
                "headline": "NVDA earnings preview",
                "summary": "anticipation",
                "date": news_date.isoformat(),
                "source_url": "https://news.example.com/nvda",
            }
        ],
        "pulled_at": datetime.now(UTC).isoformat(),
        "source_url": "https://finnhub.io",
    }

    async def fake_dispatcher(name, args):
        return {
            "get_earnings_calendar": earnings_payload,
            "read_sec_filings": sec_payload,
            "search_news": news_payload,
        }.get(name, {"items": [], "error": f"unknown: {name}"})

    final = json.dumps(
        {
            "ticker": "NVDA",
            "as_of": datetime.now(UTC).isoformat(),
            "no_known_catalysts": False,
            "confirmed": [
                {
                    "event_type": "earnings",
                    "event_date": earnings_date.isoformat(),
                    "description": "NVDA earnings (amc)",
                    "source_url": "https://finnhub.io",
                    "source_pulled_at": datetime.now(UTC).isoformat(),
                }
            ],
            "expected": [],
            "speculative": [],
            "news_themes": [
                {
                    "summary": "Recent 8-K filing",
                    "date": filing_date.isoformat(),
                    "source_url": "https://www.sec.gov/Archives/edgar/data/1045810/x.htm",
                },
                {
                    "summary": "Earnings preview coverage",
                    "date": news_date.isoformat(),
                    "source_url": "https://news.example.com/nvda",
                },
            ],
            "flags": [],
        }
    )

    earnings_block = _Block(
        "tool_use", name="get_earnings_calendar", input={"ticker": "NVDA"}, id="t1"
    )
    sec_block = _Block("tool_use", name="read_sec_filings", input={"ticker": "NVDA"}, id="t2")
    news_block = _Block("tool_use", name="search_news", input={"query": "NVDA"}, id="t3")
    fake = _FakeAnthropic(
        [
            _Response(
                content=[earnings_block, sec_block, news_block],
                stop_reason="tool_use",
            ),
            _text_response(final),
        ]
    )

    out = await run_catalyst_agent(
        "NVDA", "equity_us", db_path, anthropic_client=fake, tool_dispatcher=fake_dispatcher
    )

    assert out.ticker == "NVDA"
    assert [e.event_date for e in out.confirmed] == [earnings_date]
    news_dates = sorted(n.date for n in out.news_themes)
    assert filing_date in news_dates
    assert news_date in news_dates
