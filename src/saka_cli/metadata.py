"""
sync_metadata.json read/write.

The GUI reads this file to render the chat list without walking the whole
message tree (see SakaDesk's ``backend/api/content.py::load_sync_metadata``),
so the CLI has to produce the same schema:

    {
      "groups": {
        "{group_id}_{member_id}": {
          "group_id", "group_name", "group_thumbnail",
          "member_id", "member_name", "thumbnail", "portrait",
          "last_message_id", "last_sync_ts"
        }
      },
      "server_groups": {
        "{group_id}": {"state", "is_active", "unread_count"}
      },
      "last_sync": "<iso8601 utc>"
    }

Writes are atomic (temp file + ``os.replace``) so a crash mid-write cannot
leave the GUI a truncated file, matching ``SyncService.save_metadata``.
"""

import json
import os
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import structlog

logger = structlog.get_logger(__name__)


def empty_metadata() -> dict[str, Any]:
    return {"groups": {}, "last_sync": None}


async def load_metadata(metadata_file: Path) -> dict[str, Any]:
    """Load sync_metadata.json, returning an empty skeleton if absent/corrupt."""
    if metadata_file.exists():
        try:
            async with aiofiles.open(metadata_file, encoding="utf-8") as f:
                data = json.loads(await f.read())
            data.setdefault("groups", {})
            data.setdefault("last_sync", None)
            return dict(data)
        except Exception as e:
            logger.warning(
                "Failed to load sync metadata; starting fresh",
                metadata_file=str(metadata_file),
                error=str(e),
            )
    return empty_metadata()


async def save_metadata(metadata_file: Path, metadata: dict[str, Any]) -> None:
    """Atomically write sync_metadata.json."""
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(metadata_file.parent), suffix=".tmp")
    os.close(fd)
    try:
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(metadata, ensure_ascii=False, indent=2))
        os.replace(tmp_path, str(metadata_file))
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def record_server_group(metadata: dict[str, Any], group: dict[str, Any]) -> None:
    """
    Record group-level server state.

    Mirrors SakaDesk phase 1: entries are merged, never deleted, so a group
    that disappears from the subscription list keeps its last known state.
    """
    server_groups = metadata.setdefault("server_groups", {})
    sub = group.get("subscription") or {}
    sub_state = sub.get("state")
    server_groups[str(group["id"])] = {
        "state": group.get("state", "open"),
        "is_active": sub_state in ("active", "cancelled")
        if group.get("state") != "closed"
        else False,
        # The API omits unread_count when it is zero.
        "unread_count": int(group.get("unread_count", 0)),
    }


def record_member(
    metadata: dict[str, Any], group: dict[str, Any], member: dict[str, Any]
) -> str:
    """
    Create or refresh the per-member entry and return its key.

    On an existing entry the mutable display fields are refreshed but the sync
    cursor fields are left alone — they belong to :func:`record_sync_cursor`.
    """
    key = f"{group['id']}_{member['id']}"
    groups = metadata.setdefault("groups", {})
    if key not in groups:
        groups[key] = {
            "group_id": group["id"],
            "group_name": group.get("name"),
            "group_thumbnail": group.get("thumbnail"),
            "member_id": member["id"],
            "member_name": member.get("name"),
            "last_message_id": None,
            "thumbnail": member.get("thumbnail"),
            "portrait": member.get("portrait"),
        }
    else:
        groups[key]["group_name"] = group.get("name")
        groups[key]["member_name"] = member.get("name")
        groups[key]["thumbnail"] = member.get("thumbnail")
        groups[key]["portrait"] = member.get("portrait")
        groups[key]["group_thumbnail"] = group.get("thumbnail")
    return key


def record_sync_cursor(
    metadata: dict[str, Any], key: str, last_id: Any, last_ts: Any
) -> None:
    """Store the post-sync cursor for a member entry."""
    groups = metadata.get("groups", {})
    if key in groups and (last_id or last_ts):
        groups[key]["last_message_id"] = last_id
        groups[key]["last_sync_ts"] = last_ts


def stamp_last_sync(metadata: dict[str, Any]) -> None:
    metadata["last_sync"] = datetime.now(timezone.utc).isoformat()
