"""
Message sync, mirroring SakaDesk's three-phase pipeline.

Phase 1 discovers groups/members and seeds ``sync_metadata.json``.
Phase 2 fetches each group's timeline **once** and distributes it to that
group's members in memory, instead of one API call per member.
Phase 3 drains the media queue and folds the extracted dimensions/duration
back into each ``messages.json``.

The files themselves are written by pysaka's ``SyncManager``, which is handed
the per-service root (``{output_dir}/日向坂46``) so ``messages/`` and
``sync_state.json`` land exactly where the GUI puts them.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import structlog
from pysaka import RefreshFailedError, SessionExpiredError, SyncManager

from saka_cli import metadata as meta_store

logger = structlog.get_logger(__name__)

# Cap on how much history a member's *first* sync pulls. Matches SakaDesk's
# DEFAULT_INITIAL_MESSAGE_LIMIT so both tools produce the same messages.json
# on a fresh output tree.
DEFAULT_INITIAL_MESSAGE_LIMIT = 1000

# Re-fetch window applied to the group cursor, to catch messages that landed
# on the boundary of the previous run.
SYNC_OVERLAP_SECONDS = 300

# Media downloads are drained in chunks so progress advances steadily.
MEDIA_CHUNK_SIZE = 50
MEDIA_CONCURRENCY = 5


def _overlapped_cursor(min_ts: str) -> str:
    """Back the cursor off by the overlap window; fall back to it unchanged."""
    try:
        dt = datetime.fromisoformat(min_ts.replace("Z", "+00:00"))
        return (dt - timedelta(seconds=SYNC_OVERLAP_SECONDS)).isoformat().replace(
            "+00:00", "Z"
        )
    except (ValueError, TypeError):
        return min_ts


async def discover_tasks(
    session: aiohttp.ClientSession,
    client,
    metadata: dict[str, Any],
    group_ids: Iterable[int] | None = None,
    member_ids: Iterable[Any] | None = None,
    include_offline: bool = False,
    on_group_done: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Phase 1: resolve the (group, member) work list and seed metadata.

    Closed groups are skipped — their timeline and member endpoints 404.
    """
    if group_ids:
        wanted = set(group_ids)
        all_groups = await client.get_groups(session, include_inactive=True)
        groups = [g for g in all_groups if g["id"] in wanted]
        # A group the user named but isn't subscribed to still gets a stub so
        # the sync can be attempted rather than silently dropped.
        found = {g["id"] for g in groups}
        groups.extend({"id": gid, "name": str(gid)} for gid in wanted - found)
    else:
        groups = await client.get_groups(session, include_inactive=include_offline)

    for g in groups:
        meta_store.record_server_group(metadata, g)

    target_member_ids = {str(m) for m in member_ids} if member_ids else None

    tasks: list[dict[str, Any]] = []
    for g in groups:
        if g.get("state") == "closed":
            if on_group_done:
                on_group_done()
            continue

        members = await client.get_members(session, g["id"])
        if target_member_ids is not None:
            members = [m for m in members if str(m["id"]) in target_member_ids]

        for m in members:
            tasks.append({"group": g, "member": m})
            meta_store.record_member(metadata, g, m)

        if on_group_done:
            on_group_done()

    return tasks


async def sync_messages(
    session: aiohttp.ClientSession,
    manager: SyncManager,
    tasks: list[dict[str, Any]],
    media_queue: list[dict[str, Any]],
    metadata: dict[str, Any],
    initial_limit: int | None = DEFAULT_INITIAL_MESSAGE_LIMIT,
    on_member_done: Callable[[str, int], None] | None = None,
) -> tuple[int, int]:
    """
    Phase 2: sync every member, one timeline fetch per group.

    Returns ``(total_new_messages, members_with_new_messages)``.
    """
    tasks_by_group: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        tasks_by_group[task["group"]["id"]].append(task)

    async def sync_group(
        gid: int, group_tasks: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], int]]:
        since_timestamps = [
            manager.get_last_ts(gid, t["member"]["id"]) for t in group_tasks
        ]
        synced_ts = [s for s in since_timestamps if s is not None]
        none_count = len(since_timestamps) - len(synced_ts)

        # A group mixing synced and unsynced members must be fetched in full:
        # min(synced cursors) would only return recent messages, but an
        # unsynced member needs its complete history — sync_member records its
        # cursor from what it writes, so anything missing here is lost for good.
        if synced_ts and none_count == 0:
            min_since_ts: str | None = _overlapped_cursor(min(synced_ts))
        else:
            min_since_ts = None

        # ONE API call for the whole group.
        all_messages = await manager.client.get_messages(
            session, gid, since_ts=min_since_ts
        )

        group_results: list[tuple[dict[str, Any], int]] = []
        for task, member_ts in zip(group_tasks, since_timestamps):
            member_id = task["member"]["id"]
            member_prefetched = all_messages

            # First sync of a cursor-less member: get_messages has no
            # server-side count cap, so slice this member's newest N out of
            # the group timeline. sync_member takes its cursor from the newest
            # message it writes, so the cursor stays correct; older history
            # beyond the cap is intentionally not fetched.
            if member_ts is None and initial_limit and initial_limit > 0:
                member_msgs = [
                    m for m in all_messages if m.get("member_id") == member_id
                ]
                if len(member_msgs) > initial_limit:
                    member_msgs.sort(
                        key=lambda m: (m.get("published_at") or "", m["id"])
                    )
                    member_prefetched = member_msgs[-initial_limit:]

            count = await manager.sync_member(
                session,
                task["group"],
                task["member"],
                media_queue,
                prefetched_messages=member_prefetched,
            )
            if on_member_done:
                on_member_done(task["member"].get("name", str(member_id)), count)
            group_results.append((task, count))

        return group_results

    # return_exceptions so one group's failure doesn't propagate mid-flight and
    # tear the session down under sibling coroutines that are still writing.
    group_ids = list(tasks_by_group.keys())
    all_group_results = await asyncio.gather(
        *[sync_group(gid, tasks_by_group[gid]) for gid in group_ids],
        return_exceptions=True,
    )

    results: list[tuple[dict[str, Any], int]] = []
    group_errors: list[tuple[int, BaseException]] = []
    for gid, res in zip(group_ids, all_group_results):
        # Session expiry has to surface so the caller can prompt a re-login.
        if isinstance(res, (SessionExpiredError, RefreshFailedError)):
            raise res
        if isinstance(res, BaseException):
            group_errors.append((gid, res))
            logger.error(
                "group_sync_failed",
                group_id=gid,
                error=str(res),
                error_type=type(res).__name__,
            )
            continue
        results.extend(res)

    # Only fail the run outright if every group failed; a partial failure still
    # keeps whatever the healthy groups wrote.
    if group_errors and not results:
        first_gid, first_err = group_errors[0]
        raise RuntimeError(
            f"All {len(group_errors)} group(s) failed to sync; "
            f"first error (group {first_gid}): {first_err}"
        )

    total_new = 0
    members_with_new = 0
    for task, count in results:
        if count <= 0:
            continue
        total_new += count
        members_with_new += 1
        gid = task["group"]["id"]
        mid = task["member"]["id"]
        meta_store.record_sync_cursor(
            metadata,
            f"{gid}_{mid}",
            manager.get_last_id(gid, mid),
            manager.get_last_ts(gid, mid),
        )

    return total_new, members_with_new


async def download_media(
    session: aiohttp.ClientSession,
    manager: SyncManager,
    media_queue: list[dict[str, Any]],
    on_chunk_done: Callable[[int], None] | None = None,
) -> int:
    """
    Phase 3: drain the media queue and write the extracted metadata back.

    Dimensions, duration and mute flags are collected per member directory and
    folded into that member's ``messages.json`` once the queue is drained, so
    the GUI can lay out media without probing the files itself.
    """
    if not media_queue:
        return 0

    all_dimensions_by_dir: dict[Path, dict[int, dict[str, Any]]] = {}

    for i in range(0, len(media_queue), MEDIA_CHUNK_SIZE):
        chunk = media_queue[i : i + MEDIA_CHUNK_SIZE]
        chunk_dimensions = await manager.process_media_queue(
            session, chunk, concurrency=MEDIA_CONCURRENCY
        )
        for member_dir, dims in chunk_dimensions.items():
            all_dimensions_by_dir.setdefault(member_dir, {}).update(dims)
        if on_chunk_done:
            on_chunk_done(len(chunk))

    for member_dir, meta in all_dimensions_by_dir.items():
        await manager.update_message_metadata(member_dir / "messages.json", meta)

    return len(media_queue)
