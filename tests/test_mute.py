"""/mute and /unmute end-to-end + the duration parser."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.db.schema import init_db
from src.db.watchlist import (
    add_entry,
    clear_mute,
    find_entry,
    is_muted,
    mute_remaining,
    set_mute,
)
from src.telegram.commands import cmd_mute, cmd_unmute
from src.telegram.formatters import format_watchlist
from src.utils.timestamps import fmt_duration, parse_duration

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


def test_parse_duration_minutes() -> None:
    assert parse_duration("30m") == timedelta(minutes=30)


def test_parse_duration_hours() -> None:
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("1H") == timedelta(hours=1)  # case-insensitive


def test_parse_duration_days() -> None:
    assert parse_duration("7d") == timedelta(days=7)


def test_parse_duration_with_whitespace() -> None:
    assert parse_duration("  24h  ") == timedelta(hours=24)


def test_parse_duration_rejects_zero() -> None:
    assert parse_duration("0h") is None
    assert parse_duration("0") is None


def test_parse_duration_rejects_garbage() -> None:
    for raw in ("", "abc", "10", "10x", "h24", "-1h", "1.5h", "1 hour"):
        assert parse_duration(raw) is None


# ---------------------------------------------------------------------------
# fmt_duration
# ---------------------------------------------------------------------------


def test_fmt_duration_buckets() -> None:
    assert fmt_duration(timedelta(seconds=30)) == "30s"
    assert fmt_duration(timedelta(minutes=5)) == "5m"
    assert fmt_duration(timedelta(hours=3)) == "3h"
    assert fmt_duration(timedelta(days=2)) == "2d"


def test_fmt_duration_negative_renders_as_zero() -> None:
    assert fmt_duration(timedelta(seconds=-100)) == "0s"


# ---------------------------------------------------------------------------
# DB-level mute mutators
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "mute_test.db"
    await init_db(p)
    return p


@pytest.mark.asyncio
async def test_set_mute_updates_row(db_path: Path) -> None:
    await add_entry(db_path, 1, "NVDA", "equity_us")
    until = datetime.now(UTC) + timedelta(hours=24)
    rows = await set_mute(db_path, 1, "NVDA", until)
    assert rows == 1
    entry = await find_entry(db_path, 1, "NVDA")
    assert is_muted(entry)
    rem = mute_remaining(entry)
    assert rem is not None
    assert timedelta(hours=23, minutes=59) <= rem <= timedelta(hours=24)


@pytest.mark.asyncio
async def test_set_mute_unknown_ticker_returns_zero(db_path: Path) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    assert await set_mute(db_path, 1, "NOPE", until) == 0


@pytest.mark.asyncio
async def test_clear_mute_unmutes(db_path: Path) -> None:
    await add_entry(db_path, 1, "NVDA", "equity_us")
    await set_mute(db_path, 1, "NVDA", datetime.now(UTC) + timedelta(hours=1))
    assert is_muted(await find_entry(db_path, 1, "NVDA"))
    rows = await clear_mute(db_path, 1, "NVDA")
    assert rows == 1
    assert not is_muted(await find_entry(db_path, 1, "NVDA"))


@pytest.mark.asyncio
async def test_clear_mute_idempotent(db_path: Path) -> None:
    await add_entry(db_path, 1, "NVDA", "equity_us")
    assert await clear_mute(db_path, 1, "NVDA") == 1  # row exists
    assert await clear_mute(db_path, 1, "NVDA") == 1  # still affects 1 row (row exists, no error)


@pytest.mark.asyncio
async def test_mute_remaining_returns_none_after_expiry(db_path: Path) -> None:
    await add_entry(db_path, 1, "NVDA", "equity_us")
    past = datetime.now(UTC) - timedelta(hours=1)
    await set_mute(db_path, 1, "NVDA", past)
    entry = await find_entry(db_path, 1, "NVDA")
    assert mute_remaining(entry) is None
    assert is_muted(entry) is False


# ---------------------------------------------------------------------------
# /mute and /unmute commands
# ---------------------------------------------------------------------------


class _M:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class _U:
    def __init__(self):
        from types import SimpleNamespace

        self.effective_user = SimpleNamespace(id=12345)
        self.effective_chat = SimpleNamespace(id=12345)
        self.effective_message = _M()


class _C:
    def __init__(self, args):
        self.args = args


@pytest.mark.asyncio
async def test_cmd_mute_happy_path(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)

    await add_entry(db_path, 1, "BTCUSDT", "crypto", "binance")
    update = _U()
    ctx = _C(["BTCUSDT", "24h"])
    await cmd_mute(update, ctx)
    text = update.effective_message.replies[0][0]
    assert "Muted" in text
    assert "BTCUSDT" in text
    # fmt_duration shows the largest fitting unit, so 24h renders as "1d".
    assert "1d" in text
    assert is_muted(await find_entry(db_path, 1, "BTCUSDT"))


@pytest.mark.asyncio
async def test_cmd_mute_usage(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)
    update = _U()
    ctx = _C([])
    await cmd_mute(update, ctx)
    assert "Usage: /mute" in update.effective_message.replies[0][0]


@pytest.mark.asyncio
async def test_cmd_mute_bad_duration(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)
    update = _U()
    ctx = _C(["BTCUSDT", "tomorrow"])
    await cmd_mute(update, ctx)
    assert "Bad duration" in update.effective_message.replies[0][0]


@pytest.mark.asyncio
async def test_cmd_mute_not_on_watchlist(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)
    update = _U()
    ctx = _C(["NOPE", "1h"])
    await cmd_mute(update, ctx)
    assert "not on the watchlist" in update.effective_message.replies[0][0]


@pytest.mark.asyncio
async def test_cmd_unmute_happy_path(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)
    await add_entry(db_path, 1, "BTCUSDT", "crypto", "binance")
    await set_mute(db_path, 1, "BTCUSDT", datetime.now(UTC) + timedelta(hours=1))

    update = _U()
    ctx = _C(["BTCUSDT"])
    await cmd_unmute(update, ctx)
    assert "Unmuted" in update.effective_message.replies[0][0]
    assert not is_muted(await find_entry(db_path, 1, "BTCUSDT"))


@pytest.mark.asyncio
async def test_cmd_unmute_unknown_ticker(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)
    update = _U()
    ctx = _C(["NOPE"])
    await cmd_unmute(update, ctx)
    assert "not on the watchlist" in update.effective_message.replies[0][0]


# ---------------------------------------------------------------------------
# format_watchlist shows remaining mute time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_format_watchlist_includes_mute_remaining(db_path: Path) -> None:
    await add_entry(db_path, 1, "BTCUSDT", "crypto", "binance")
    await set_mute(db_path, 1, "BTCUSDT", datetime.now(UTC) + timedelta(hours=24))
    from src.db.watchlist import list_entries

    entries = await list_entries(db_path, 1)
    text = format_watchlist(entries)
    assert "🔴" in text
    assert "muted" in text
    # 24h shown in some bucket; allow a little slop for clock drift.
    assert any(tag in text for tag in ("23h", "24h"))
