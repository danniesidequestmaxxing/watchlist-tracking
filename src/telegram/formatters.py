import logging
from datetime import UTC, datetime
from html import escape
from urllib.parse import urlparse

from src.catalyst.agent import CatalystEvent, CatalystOutput, NewsTheme
from src.db.watchlist import WatchlistEntry
from src.ta.pipeline import TASnapshot, validate_ta_snapshot

logger = logging.getLogger(__name__)


def format_watchlist(entries: list[WatchlistEntry]) -> str:
    if not entries:
        return "📋 Watchlist empty. Add entries with /add &lt;ticker&gt; [exchange]."

    lines = [f"📋 <b>Watchlist ({len(entries)} active)</b>", ""]
    for entry in entries:
        is_muted = entry.muted_until is not None
        marker = "🔴" if is_muted else "🟢"
        ticker = escape(entry.ticker)
        exch = f" ({escape(entry.exchange)})" if entry.exchange else ""
        suffix = "  muted" if is_muted else ""
        lines.append(f"{marker} <b>{ticker}</b>{exch} — {entry.asset_class}{suffix}")
    return "\n".join(lines)


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    if value >= 0.01:
        return f"{value:.4f}"
    return f"{value:.8f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def _fmt_relative(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _trend_summary(snap: TASnapshot) -> str:
    above: list[str] = []
    below: list[str] = []
    for label, flag in (
        ("EMA20", snap.above_ema_20),
        ("EMA50", snap.above_ema_50),
        ("SMA200", snap.above_sma_200),
    ):
        if flag is True:
            above.append(label)
        elif flag is False:
            below.append(label)
    parts = []
    if above:
        parts.append("above " + "/".join(above))
    if below:
        parts.append("below " + "/".join(below))
    if not parts:
        return "insufficient bars"
    if above and below:
        return ", ".join(parts) + " (mixed)"
    if above and not below:
        return ", ".join(parts) + " (bullish)"
    return ", ".join(parts) + " (bearish)"


def _rsi_label(snap: TASnapshot) -> str:
    if snap.rsi_14 is None:
        return "RSI(14): n/a"
    if snap.rsi_overbought:
        tag = "overbought"
    elif snap.rsi_oversold:
        tag = "oversold"
    else:
        tag = "neutral"
    return f"RSI(14): {snap.rsi_14:.0f} ({tag})"


def _macd_label(snap: TASnapshot) -> str:
    if snap.macd_line is None:
        return "MACD: n/a"
    parts: list[str] = []
    if snap.macd_bullish_cross:
        parts.append("bullish cross")
    elif snap.macd_bearish_cross:
        parts.append("bearish cross")
    elif snap.macd_line > (snap.macd_signal or 0):
        parts.append("above signal")
    else:
        parts.append("below signal")
    if snap.macd_hist_rising is True:
        parts.append("hist rising")
    elif snap.macd_hist_rising is False:
        parts.append("hist falling")
    return "MACD: " + ", ".join(parts)


def _bb_atr_label(snap: TASnapshot) -> str:
    bb = snap.bb_position.capitalize() if snap.bb_position != "unknown" else "n/a"
    if snap.atr_14 is None:
        return f"BB: {bb}"
    return f"BB: {bb} | ATR {_fmt_price(snap.atr_14)}"


def format_ta_snapshot(snap: TASnapshot) -> str:
    issues = validate_ta_snapshot(snap)
    if issues:
        logger.warning("Refusing to format invalid TA snapshot: %s", issues)
        return (
            f"📊 <b>{escape(snap.ticker)}</b> | data unavailable "
            f"(failed validation: {escape(issues[0])})"
        )

    ticker = escape(snap.ticker)
    timeframe = escape(snap.timeframe)
    exchange = escape(snap.exchange or "n/a")

    spike = " ⚡" if snap.volume_spike else ""
    vol_ratio = f"{snap.volume_ratio:.1f}x avg{spike}" if snap.volume_ratio is not None else "n/a"

    lines = [
        f"📊 <b>{ticker}</b> | {timeframe}",
        (
            f"Price: <b>{_fmt_price(snap.price)}</b> "
            f"({_fmt_pct(snap.return_1h)} 1h, "
            f"{_fmt_pct(snap.return_24h)} 24h, "
            f"{_fmt_pct(snap.return_7d)} 7d)"
        ),
        f"Vol: {vol_ratio}",
        "",
        f"Trend: {_trend_summary(snap)}",
        _rsi_label(snap),
        _macd_label(snap),
        _bb_atr_label(snap),
        "",
        f"24h H/L: {_fmt_price(snap.high_24h)} / {_fmt_price(snap.low_24h)}",
        f"7d H/L: {_fmt_price(snap.high_7d)} / {_fmt_price(snap.low_7d)}",
        f"Pulled: {_fmt_relative(snap.pulled_at)} ({exchange})",
    ]
    return "\n".join(lines)


_MONTH_DAY = "%b %-d"


def _fmt_event_date(d) -> str:
    return d.strftime(_MONTH_DAY)


def _domain(url) -> str:
    try:
        host = urlparse(str(url)).netloc
    except Exception:
        return str(url)
    return host.removeprefix("www.")


def _fmt_event_line(event: CatalystEvent) -> str:
    desc = escape(event.description or event.event_type)
    return (
        f"• {_fmt_event_date(event.event_date)}: {desc}\n"
        f"   src: {escape(_domain(event.source_url))} "
        f"(pulled {_fmt_relative(event.source_pulled_at)})"
    )


def _fmt_news_line(item: NewsTheme) -> str:
    summary = escape(item.summary)
    stale = " (stale)" if item.stale else ""
    return (
        f"• {summary}{stale}\n"
        f"   src: {escape(_domain(item.source_url))} ({_fmt_event_date(item.date)})"
    )


def format_catalyst_output(out: CatalystOutput, *, window_days: int = 14) -> str:
    ticker = escape(out.ticker)
    if out.no_known_catalysts and not (out.confirmed or out.expected or out.speculative):
        flags = "\n".join(f"• {escape(f)}" for f in out.flags) if out.flags else ""
        flag_block = f"\n\n⚠ Flags\n{flags}" if flags else ""
        return f"🎯 <b>{ticker}</b> | no known catalysts{flag_block}"

    lines: list[str] = [f"🎯 <b>{ticker}</b> | Catalysts (next {window_days}d)"]

    if out.confirmed:
        lines.append("\n<b>CONFIRMED</b>")
        lines.extend(_fmt_event_line(e) for e in out.confirmed)
    if out.expected:
        lines.append("\n<b>EXPECTED</b>")
        lines.extend(_fmt_event_line(e) for e in out.expected)
    if out.speculative:
        lines.append("\n<b>SPECULATIVE</b>")
        lines.extend(_fmt_event_line(e) for e in out.speculative)
    if out.news_themes:
        lines.append("\n<b>NEWS THEMES (7d)</b>")
        lines.extend(_fmt_news_line(n) for n in out.news_themes)
    if out.flags:
        lines.append("\n⚠ <b>Flags</b>")
        lines.extend(f"• {escape(f)}" for f in out.flags)

    return "\n".join(lines)


# Phase 8 — /health
SOURCE_TTL_HOURS: dict[str, int] = {
    "ccxt:binance": 2,
    "ccxt:okx": 2,
    "ccxt:hyperliquid": 2,
    "yfinance": 4,
    "token-unlocks": 30,
    "trading-economics": 30,
    "finnhub": 8,
    "sec-edgar": 30,
    "catalyst-agent": 48,
}


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _row_for(source: str, info: dict | None, ttl_hours: int) -> str:
    label = source.ljust(22)
    if info is None:
        return f"<code>{escape(label)}</code> ❌ no data"
    age_seconds = (datetime.now(UTC) - info["recorded_at"]).total_seconds()
    age_str = _fmt_age(age_seconds)
    status = info["status"]
    details = (info.get("details") or "").strip()
    if status == "error":
        suffix = f" (last error: {escape(details[:80])})" if details else ""
        return f"<code>{escape(label)}</code> ❌ {age_str}{suffix}"
    if status == "rate_limited":
        return f"<code>{escape(label)}</code> ⚠ {age_str} (rate limited)"
    if age_seconds > ttl_hours * 3600:
        return f"<code>{escape(label)}</code> ⚠ {age_str} (TTL {ttl_hours}h, stale)"
    return f"<code>{escape(label)}</code> ✅ {age_str}"


def format_health(per_source: dict[str, dict]) -> str:
    """Render the spec §5.6 /health block.

    Sources known to the bot are listed in their canonical order; any other
    sources present in the table get appended at the bottom under a 24h TTL.
    """
    lines = ["🩺 <b>Source health</b>", ""]
    seen: set[str] = set()

    for source, ttl in SOURCE_TTL_HOURS.items():
        seen.add(source)
        lines.append(_row_for(source, per_source.get(source), ttl))

    extras = sorted(s for s in per_source if s not in seen)
    if extras:
        lines.append("")
        for source in extras:
            lines.append(_row_for(source, per_source[source], ttl_hours=24))

    return "\n".join(lines)
