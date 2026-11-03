"""Tests for SakaCLI._sync_room: per-room Phase 2 (timeline fetch + member sync)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs
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


async def test_extra_members_api_error_is_not_fatal_while_scanning():
    cli, manager = make_cli(scanned=False, complete=False)
    manager.extra_room_members.side_effect = ApiError("boom", 500)

    await run_room(cli)

    assert manager.sync_member.await_count == 1
    manager.mark_history_scanned.assert_not_called()


async def test_extra_members_error_on_scanned_room_fails_before_any_sync():
    """Syncing only the listed members would advance their cursors past the
    extras' messages, which an incremental fetch never returns again."""
    cli, manager = make_cli(scanned=True, last_id=500)
    manager.extra_room_members.side_effect = RateLimitedError("slow down")

    with pytest.raises(RateLimitedError):
        await run_room(cli)

    manager.sync_member.assert_not_awaited()
    manager.mark_history_scanned.assert_not_called()


async def test_scans_never_overlap_across_rooms_sharing_scan_sem():
    events = []

    async def get_messages(session, gid, **kwargs):
        events.append(("start", gid))
        await asyncio.sleep(0.01)
        events.append(("end", gid))
        return MESSAGES

    cli, manager = make_cli(scanned=False, extras=[])
    manager.client.get_messages = get_messages
    scan_sem = asyncio.Semaphore(1)

    async def room(gid):
        await cli._sync_room(
            "session",
            gid,
            [{"group": {"id": gid, "name": "r"}, "member": MEMBER_79}],
            [],
            {},
            scan_sem,
            MagicMock(total=1),
        )

    await asyncio.gather(room(1), room(2))

    assert [e[0] for e in events] == ["start", "end", "start", "end"]
    assert events[0][1] == events[1][1]


async def test_auth_error_from_extra_members_propagates():
    cli, manager = make_cli(scanned=False)
    manager.extra_room_members.side_effect = SessionExpiredError("expired")

    with pytest.raises(SessionExpiredError):
        await run_room(cli)

    manager.mark_history_scanned.assert_not_called()


def events_by_name(logs):
    return {entry["event"]: entry for entry in logs}


async def test_scan_logs_breadcrumbs_without_message_content():
    cli, manager = make_cli(scanned=False, complete=True)

    with capture_logs() as logs:
        await run_room(cli)

    events = events_by_name(logs)
    assert events["room_scan_started"]["group_id"] == GID
    assert events["room_extra_members_found"]["member_ids"] == [22, 0]
    assert events["room_extra_members_found"]["count"] == 2
    finished = events["room_scan_finished"]
    assert finished["group_id"] == GID
    assert finished["fetched"] == len(MESSAGES)
    assert finished["extras"] == 2
    assert finished["marked"] is True
    assert isinstance(finished["elapsed_s"], float)
    assert "room_scan_incomplete" not in events


async def test_incomplete_scan_logs_warning():
    cli, manager = make_cli(scanned=False, complete=False)

    with capture_logs() as logs:
        await run_room(cli)

    events = events_by_name(logs)
    assert events["room_scan_incomplete"]["group_id"] == GID
    assert events["room_scan_incomplete"]["log_level"] == "warning"
    assert events["room_scan_finished"]["marked"] is False


async def test_extras_failure_logs_scanned_and_action():
    cli, manager = make_cli(scanned=False, complete=False)
    manager.extra_room_members.side_effect = ApiError("boom", 500)
    with capture_logs() as logs:
        await run_room(cli)
    failed = events_by_name(logs)["extra_room_members_failed"]
    assert failed["error_type"] == "ApiError"
    assert failed["scanned"] is False
    assert failed["action"] == "continue"
    assert failed["group_id"] == GID

    cli, manager = make_cli(scanned=True, last_id=500)
    manager.extra_room_members.side_effect = ApiError("boom", 500)
    with capture_logs() as logs, pytest.raises(ApiError):
        await run_room(cli)
    failed = events_by_name(logs)["extra_room_members_failed"]
    assert failed["scanned"] is True
    assert failed["action"] == "fail_room"


# ---------------------------------------------------------------------------
# _sync_rooms: fan-out over rooms, failure isolation
# ---------------------------------------------------------------------------

GROUP_2 = {"id": 44, "name": "second room"}
MEMBER_5 = {"id": 5, "name": "member 5"}
TASKS_TWO_ROOMS = [
    {"group": GROUP, "member": MEMBER_79},
    {"group": GROUP_2, "member": MEMBER_5},
]


def make_two_room_cli(*, fail_gid, error):
    """Both rooms are already scanned; get_messages fails for ``fail_gid``."""
    cli, manager = make_cli(scanned=True, extras=[], last_id=500)
    finished = []

    async def get_messages(session, gid, **kwargs):
        await asyncio.sleep(0.01 if gid == fail_gid else 0.03)
        if gid == fail_gid:
            raise error
        finished.append(gid)
        return MESSAGES

    manager.client.get_messages = get_messages
    return cli, manager, finished


async def test_failing_room_does_not_abort_other_rooms():
    cli, manager, finished = make_two_room_cli(fail_gid=43, error=ApiError("x", 500))
    media_queue = []

    with capture_logs() as logs:
        await cli._sync_rooms("session", TASKS_TWO_ROOMS, media_queue)

    assert finished == [44]  # the slower, healthy room was awaited to the end
    assert [c.args[2]["id"] for c in manager.sync_member.await_args_list] == [5]
    failed = events_by_name(logs)["room_sync_failed"]
    assert failed["group_id"] == 43
    assert failed["error_type"] == "ApiError"
    assert set(failed) == {"event", "log_level", "group_id", "error_type"}
    partial = events_by_name(logs)["rooms_sync_partial_failure"]
    assert partial["log_level"] == "warning"
    assert partial["failed"] == 1
    assert partial["group_ids"] == [43]
    assert partial["total"] == 2


async def test_auth_error_is_raised_after_all_rooms_finish():
    cli, manager, finished = make_two_room_cli(
        fail_gid=43, error=SessionExpiredError("expired")
    )

    with capture_logs() as logs, pytest.raises(SessionExpiredError):
        await cli._sync_rooms("session", TASKS_TWO_ROOMS, [])

    assert finished == [44]
    assert [c.args[2]["id"] for c in manager.sync_member.await_args_list] == [5]
    assert events_by_name(logs)["room_sync_failed"]["group_id"] == 43


async def test_all_rooms_failing_raises_first_error():
    cli, manager = make_cli(scanned=True, extras=[], last_id=500)
    manager.client.get_messages = AsyncMock(side_effect=ApiError("down", 500))

    with capture_logs() as logs, pytest.raises(ApiError):
        await cli._sync_rooms("session", TASKS_TWO_ROOMS, [])

    assert len([e for e in logs if e["event"] == "room_sync_failed"]) == 2
    manager.sync_member.assert_not_awaited()


async def test_all_rooms_failing_logs_no_partial_summary():
    cli, manager = make_cli(scanned=True, extras=[], last_id=500)
    manager.client.get_messages = AsyncMock(side_effect=ApiError("down", 500))

    with capture_logs() as logs, pytest.raises(ApiError):
        await cli._sync_rooms("session", TASKS_TWO_ROOMS, [])

    assert "rooms_sync_partial_failure" not in events_by_name(logs)


async def test_incremental_room_logs_sync_finished():
    cli, manager = make_cli(scanned=True, extras=EXTRAS[:1], last_id=500)

    with capture_logs() as logs:
        await run_room(cli)

    done = events_by_name(logs)["room_sync_finished"]
    assert done["group_id"] == GID
    assert done["since_id"] == 500
    assert done["fetched"] == len(MESSAGES)
    assert done["members"] == 2
    assert done["extras"] == 1
    assert "room_scan_started" not in events_by_name(logs)


async def test_scan_elapsed_excludes_time_queued_for_scan_sem():
    cli, manager = make_cli(scanned=False, complete=True, extras=[])
    scan_sem = asyncio.Semaphore(1)
    await scan_sem.acquire()

    async def release_later():
        await asyncio.sleep(0.2)
        scan_sem.release()

    releaser = asyncio.create_task(release_later())
    with capture_logs() as logs:
        await cli._sync_room(
            "session",
            GID,
            [{"group": GROUP, "member": MEMBER_79}],
            [],
            {},
            scan_sem,
            MagicMock(total=1),
        )
    await releaser

    finished = events_by_name(logs)["room_scan_finished"]
    assert finished["waited_s"] >= 0.1
    assert finished["elapsed_s"] < 0.1
