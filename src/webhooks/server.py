"""Phase 9 — single-endpoint aiohttp server for TradingView alert ingress.

TradingView's webhook is unauthenticated by default; we authenticate via a
URL-path token compared against `WEBHOOK_TOKEN`. Configure TradingView with
    https://<host>:<port>/webhooks/tradingview/<token>
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from src.config import WEBHOOK_PORT, WEBHOOK_TOKEN
from src.telegram.broadcast import broadcast
from src.telegram.formatters import format_tradingview_alert

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


BOT_KEY: web.AppKey = web.AppKey("bot")
TOKEN_KEY: web.AppKey = web.AppKey("webhook_token", str)


async def _read_payload(request: web.Request) -> Any:
    """Best-effort JSON parse; fall back to raw text for tolerance."""
    raw = await request.read()
    text = raw.decode("utf-8", errors="replace") if raw else ""
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def _handle_tradingview(request: web.Request) -> web.Response:
    expected = request.app[TOKEN_KEY]
    token = request.match_info.get("token", "")
    if not expected or not hmac.compare_digest(token, expected):
        logger.warning("Webhook auth rejected (token mismatch)")
        return web.Response(status=403, text="forbidden")

    payload = await _read_payload(request)
    text = format_tradingview_alert(payload)
    bot: Bot = request.app[BOT_KEY]
    try:
        sent = await broadcast(bot, text)
    except Exception as exc:
        logger.exception("Telegram broadcast failed for TradingView alert: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    if sent == 0:
        return web.json_response({"ok": False, "error": "no recipients reachable"}, status=502)
    return web.json_response({"ok": True, "delivered": sent})


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "trading-bot-webhook"})


def build_app(bot: Bot, token: str | None) -> web.Application:
    app = web.Application()
    app[BOT_KEY] = bot
    app[TOKEN_KEY] = token or ""
    app.router.add_post("/webhooks/tradingview/{token}", _handle_tradingview)
    app.router.add_get("/healthz", _handle_health)
    return app


async def start(
    bot: Bot,
    *,
    port: int = WEBHOOK_PORT,
    token: str | None = WEBHOOK_TOKEN,
) -> web.AppRunner:
    """Start the webhook server. Caller must `await runner.cleanup()` on stop."""
    if not token:
        raise RuntimeError("Refusing to start webhook server without WEBHOOK_TOKEN")
    app = build_app(bot, token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Webhook server listening on :%d", port)
    return runner
