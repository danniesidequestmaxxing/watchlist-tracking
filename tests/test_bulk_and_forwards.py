"""Bulk-add (`/addmany`) + forward fan-out (`/grant` / `/revoke` / `/forwards`)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.db import forwards
from src.db.schema import init_db
from src.db.watchlist import count_entries, list_entries
from src.telegram import broadcast as broadcast_mod
from src.telegram.broadcast import broadcast
from src.telegram.commands import (
    _parse_bulk_token,
    cmd_addmany,
    cmd_forwards,
    cmd_grant,
    cmd_on_new_chat_members,
    cmd_revoke,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBot:
    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[dict] = []
        self.fail_for = fail_for or set()
        self.id = 9999  # bot_id for new-chat-member detection

    async def send_message(self, **kwargs):
        if kwargs.get("chat_id") in self.fail_for:
            raise RuntimeError(f"simulated failure for {kwargs['chat_id']}")
        self.sent.append(kwargs)


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid


class _FakeChat:
    def __init__(self, cid: int, title: str | None = None):
        self.id = cid
        self.title = title
        self.username = None
        self.full_name = None


class _FakeMessage:
    def __init__(self, new_chat_members=None):
        self.new_chat_members = new_chat_members or []
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _FakeUpdate:
    def __init__(
        self,
        *,
        owner_id: int = 12345,
        chat_id: int = 1,
        msg=None,
        chat: _FakeChat | None = None,
        new_members=None,
    ):
        self.effective_user = _FakeUser(owner_id)
        self.effective_chat = chat or _FakeChat(chat_id)
        if msg is None:
            msg = _FakeMessage(new_chat_members=new_members)
        self.effective_message = msg


class _FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args
        self.bot = bot or _FakeBot()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "bulk_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# /addmany — token parser + command
# ---------------------------------------------------------------------------


def test_parse_bulk_token_with_exchange() -> None:
    assert _parse_bulk_token("BTCUSDT:binance") == ("BTCUSDT", "binance")


def test_parse_bulk_token_no_exchange() -> None:
    assert _parse_bulk_token("NVDA") == ("NVDA", None)


def test_parse_bulk_token_uppercase_normalization() -> None:
    assert _parse_bulk_token("btcusdt:BINANCE") == ("BTCUSDT", "binance")


def test_parse_bulk_token_empty_exchange() -> None:
    assert _parse_bulk_token("NVDA:") == ("NVDA", None)


def test_parse_bulk_token_with_dot_ticker() -> None:
    assert _parse_bulk_token("DBS.SI") == ("DBS.SI", None)


@pytest.mark.asyncio
async def test_addmany_mixed_outcomes(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)

    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext(
        args=[
            "BTCUSDT:binance",
            "ETHUSDT:binance",
            "NVDA",
            "5347",
            "ZZZZZZ",  # 6 letters → won't classify
            "BTCUSDT:binance",  # duplicate
        ]
    )
    await cmd_addmany(update, ctx)
    text = update.effective_message.replies[0][0]
    assert "4 added" in text  # BTCUSDT, ETHUSDT, NVDA, 5347
    assert "1 skipped" in text  # second BTCUSDT
    assert "1 failed" in text  # ZZZZZZ
    assert "0 capped" in text
    assert await count_entries(db_path, 1) == 4


@pytest.mark.asyncio
async def test_addmany_respects_watchlist_cap(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)
    monkeypatch.setattr(cmds, "WATCHLIST_LIMIT", 3)

    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext(args=["AAA", "BBB", "CCC", "DDD", "EEE"])
    await cmd_addmany(update, ctx)
    text = update.effective_message.replies[0][0]
    assert "3 added" in text
    assert "2 capped" in text
    assert await count_entries(db_path, 1) == 3


@pytest.mark.asyncio
async def test_addmany_empty_args_shows_usage(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)

    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext(args=[])
    await cmd_addmany(update, ctx)
    assert "Usage: /addmany" in update.effective_message.replies[0][0]


@pytest.mark.asyncio
async def test_addmany_does_not_duplicate_within_one_call(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same ticker+exchange listed twice in a single /addmany only inserts once."""
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)

    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext(args=["BTCUSDT:binance", "BTCUSDT:binance", "BTCUSDT:binance"])
    await cmd_addmany(update, ctx)
    text = update.effective_message.replies[0][0]
    assert "1 added" in text
    assert "2 skipped" in text
    entries = await list_entries(db_path, 1)
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# forward target CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_add_remove_list(db_path: Path) -> None:
    assert await forwards.add_target(db_path, -1001234567890, "Trading group") is True
    assert await forwards.add_target(db_path, -1001234567890, "dup") is False  # dedup
    assert await forwards.add_target(db_path, -1009876543210, "Friends") is True

    targets = await forwards.list_targets(db_path)
    assert {t["chat_id"] for t in targets} == {-1001234567890, -1009876543210}

    assert await forwards.remove_target(db_path, -1001234567890) == 1
    assert await forwards.remove_target(db_path, -1001234567890) == 0  # already gone

    final = await forwards.list_targets(db_path)
    assert {t["chat_id"] for t in final} == {-1009876543210}


@pytest.mark.asyncio
async def test_target_chat_ids_returns_list_of_ints(db_path: Path) -> None:
    await forwards.add_target(db_path, -1001234567890, "g")
    ids = await forwards.target_chat_ids(db_path)
    assert ids == [-1001234567890]
    assert all(isinstance(x, int) for x in ids)


# ---------------------------------------------------------------------------
# /grant /revoke /forwards commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_grant_persists(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)

    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext(args=["-1001234567890", "Trading", "group"])
    await cmd_grant(update, ctx)
    text = update.effective_message.replies[0][0]
    assert "Granted" in text
    targets = await forwards.list_targets(db_path)
    assert targets[0]["chat_id"] == -1001234567890
    assert targets[0]["label"] == "Trading group"


@pytest.mark.asyncio
async def test_cmd_grant_rejects_bad_id(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext(args=["abc"])
    await cmd_grant(update, ctx)
    assert "Bad chat_id" in update.effective_message.replies[0][0]


@pytest.mark.asyncio
async def test_cmd_revoke_removes(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    await forwards.add_target(db_path, -1001234567890, "g")

    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext(args=["-1001234567890"])
    await cmd_revoke(update, ctx)
    assert "Revoked" in update.effective_message.replies[0][0]
    assert await forwards.list_targets(db_path) == []


@pytest.mark.asyncio
async def test_cmd_forwards_empty(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext()
    await cmd_forwards(update, ctx)
    assert "No additional forward targets" in update.effective_message.replies[0][0]


@pytest.mark.asyncio
async def test_cmd_forwards_lists(db_path: Path, monkeypatch) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "DB_PATH", db_path)
    await forwards.add_target(db_path, -1001234567890, "Trading group")
    update = _FakeUpdate(owner_id=12345)
    ctx = _FakeContext()
    await cmd_forwards(update, ctx)
    text = update.effective_message.replies[0][0]
    assert "-1001234567890" in text
    assert "Trading group" in text


# ---------------------------------------------------------------------------
# broadcast() fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_fans_out_to_owner_and_targets(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broadcast_mod, "DB_PATH", db_path)
    monkeypatch.setattr(broadcast_mod, "OWNER_TELEGRAM_ID", 1)

    await forwards.add_target(db_path, -1001234567890, "Trading group")
    await forwards.add_target(db_path, -1009876543210, "Friends")

    bot = _FakeBot()
    sent = await broadcast(bot, "hello")
    assert sent == 3
    chat_ids = [m["chat_id"] for m in bot.sent]
    assert set(chat_ids) == {1, -1001234567890, -1009876543210}


@pytest.mark.asyncio
async def test_broadcast_owner_dedupe(db_path: Path, monkeypatch) -> None:
    """If a forward target chat_id matches OWNER_TELEGRAM_ID, only send once."""
    monkeypatch.setattr(broadcast_mod, "DB_PATH", db_path)
    monkeypatch.setattr(broadcast_mod, "OWNER_TELEGRAM_ID", 1)

    await forwards.add_target(db_path, 1, "self")  # weird but possible

    bot = _FakeBot()
    sent = await broadcast(bot, "hello")
    assert sent == 1


@pytest.mark.asyncio
async def test_broadcast_swallows_per_target_failures(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(broadcast_mod, "DB_PATH", db_path)
    monkeypatch.setattr(broadcast_mod, "OWNER_TELEGRAM_ID", 1)

    await forwards.add_target(db_path, -1001234567890, "broken")
    await forwards.add_target(db_path, -1009876543210, "ok")

    bot = _FakeBot(fail_for={-1001234567890})
    sent = await broadcast(bot, "hello")
    assert sent == 2  # OWNER + the working one
    chat_ids = [m["chat_id"] for m in bot.sent]
    assert set(chat_ids) == {1, -1009876543210}


# ---------------------------------------------------------------------------
# new-chat-members handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_new_chat_members_dms_owner_when_bot_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)

    bot = _FakeBot()
    chat = _FakeChat(-1001234567890, title="Trading group")
    new_member = MagicMock()
    new_member.id = bot.id  # the bot itself

    update = _FakeUpdate(chat=chat, new_members=[new_member])
    ctx = _FakeContext(bot=bot)
    await cmd_on_new_chat_members(update, ctx)

    # Owner got the DM with the chat_id
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent["chat_id"] == 1
    assert "-1001234567890" in sent["text"]
    assert "Trading group" in sent["text"]
    assert "/grant" in sent["text"]


@pytest.mark.asyncio
async def test_on_new_chat_members_ignores_other_member_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random user joining a chat shouldn't trigger an owner DM."""
    from src.telegram import commands as cmds

    monkeypatch.setattr(cmds, "OWNER_TELEGRAM_ID", 1)

    bot = _FakeBot()
    chat = _FakeChat(-1001234567890, title="Trading group")
    other = MagicMock()
    other.id = 555  # not the bot

    update = _FakeUpdate(chat=chat, new_members=[other])
    ctx = _FakeContext(bot=bot)
    await cmd_on_new_chat_members(update, ctx)
    assert bot.sent == []
