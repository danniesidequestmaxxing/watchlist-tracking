from html import escape

from src.db.watchlist import WatchlistEntry


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
