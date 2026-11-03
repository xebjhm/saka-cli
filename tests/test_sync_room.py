"""Tests for SakaCLI._sync_room: per-room Phase 2 (timeline fetch + member sync)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pysaka import ApiError, Group, RateLimitedError, SessionExpiredError

from saka_cli.cli import SakaCLI

pytestmark = pytest.mark.asyncio

GID = 43
GROUP = {"id": GID, "name": "日向坂46 room"}
MEMBER_79 = {"id": 79, "name": "member 79"}
MESSAGES = [
    {"id": 1, "member_id": 79},
    {"id": 2, "member_id": 22},
    {"id": 3},  # no member_id: the room author
]
EXTRAS = [{"id": 22, "name": "x"}, {"id": 0, "name": "日向坂46"}]


def make_cli(*, scanned: bool, complete: bool = True, extras=None, last_id=None):
    cli = SakaCLI(group=Group.HINATAZAKA46)
    manager = MagicMock()
    manager.history_scanned.return_value = scanned
    manager.get_last_id.return_value = last_id
    manager.room_archive_complete = AsyncMock(return_value=complete)
    manager.extra_room_members = AsyncMock(
        return_value=EXTRAS if extras is None else extras
    )
    manager.sync_member = AsyncMock()
    manager.client.get_messages = AsyncMock(return_value=MESSAGES)
    cli.manager = manager
    return cli, manager


async def run_room(cli, pbar=None):
    pbar = pbar or MagicMock(total=1)
    await cli._sync_room(
        "session",
        GID,
        [{"group": GROUP, "member": MEMBER_79}],
        [],
        {79: MEMBER_79},
        asyncio.Semaphore(1),
        pbar,
    )
    return pbar


async def test_unscanned_room_rescans_all_authors_and_marks_scanned():
    cli, manager = make_cli(scanned=False, last_id=500)

    pbar = await run_room(cli)

    manager.client.get_messages.assert_awaited_once_with(
        "session", GID, since_id=None, page_delay=1.0
    )
    manager.extra_room_members.assert_awaited_once_with(
        "session", GROUP, [MEMBER_79], MESSAGES, {79: MEMBER_79}
    )
    synced = [c.args[2]["id"] for c in manager.sync_member.await_args_list]
    assert synced == [79, 22, 0]
    for call in manager.sync_member.await_args_list:
        assert call.kwargs["ignore_cursor"] is True
        assert call.kwargs["prefetched_messages"] is MESSAGES
    manager.mark_history_scanned.assert_called_once_with(GID)
    assert pbar.total == 3
    assert pbar.update.call_count == 3


async def test_incomplete_archive_does_not_mark_scanned():
    cli, manager = make_cli(scanned=False, complete=False)

    await run_room(cli)

    manager.room_archive_complete.assert_awaited_once_with(GROUP, MESSAGES)
    manager.mark_history_scanned.assert_not_called()


async def test_scanned_room_uses_cursor_and_default_page_delay():
    cli, manager = make_cli(scanned=True, extras=[], last_id=500)

    await run_room(cli)

    manager.client.get_messages.assert_awaited_once_with("session", GID, since_id=500)
    assert len(manager.sync_member.await_args_list) == 1
    assert manager.sync_member.await_args.kwargs["ignore_cursor"] is False
    manager.room_archive_complete.assert_not_awaited()
    manager.mark_history_scanned.assert_not_called()


async def test_extra_members_error_degrades_to_api_members_only():
    cli, manager = make_cli(scanned=False, complete=False)
    manager.extra_room_members.side_effect = RateLimitedError("slow down")

    await run_room(cli)

    assert [c.args[2]["id"] for c in manager.sync_member.await_args_list] == [79]
    manager.mark_history_scanned.assert_not_called()


async def test_extra_members_api_error_is_not_fatal():
    cli, manager = make_cli(scanned=False, complete=False)
    manager.extra_room_members.side_effect = ApiError("boom", 500)

    await run_room(cli)

    assert manager.sync_member.await_count == 1


async def test_auth_error_from_extra_members_propagates():
    cli, manager = make_cli(scanned=False)
    manager.extra_room_members.side_effect = SessionExpiredError("expired")

    with pytest.raises(SessionExpiredError):
        await run_room(cli)

    manager.mark_history_scanned.assert_not_called()
