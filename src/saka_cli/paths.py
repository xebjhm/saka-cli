"""
Disk layout for synced content.

This is the single source of truth for where the CLI writes files, and it
mirrors SakaDesk's layout exactly so both tools can share one output tree:

    {output_dir}/
      {display_name}/                       e.g. 日向坂46
        sync_metadata.json
        sync_state.json                     (written by pysaka's SyncManager)
        messages/
          {group_id} {group_name}/
            {member_id} {member_name}/
              messages.json
              picture/ video/ voice/
        blogs/
          index.json
          member_thumbnails/
            members_cache.json
            {member_id}.jpg
          {member_name}/
            {YYYYMMDD}_{blog_id}/
              blog.json
              images/img_{n}{ext}

The GUI equivalents live in SakaDesk's ``backend/services/path_resolver.py``
and ``backend/services/blog_service.py``; keep the two in step.
"""

from pathlib import Path

from pysaka import Group
from pysaka.client import GROUP_CONFIG

# Media subdirectories created inside every member directory. pysaka's
# SyncManager.sync_member creates these itself; listed here so callers can
# reason about the layout without importing the manager.
MEDIA_SUBDIRS = ("picture", "video", "voice")


def get_display_name(group: Group) -> str:
    """Japanese display name used as the per-service directory (e.g. 日向坂46)."""
    return str(GROUP_CONFIG[group].get("display_name", group.value))


def service_root(output_dir: Path, group: Group) -> Path:
    """Per-service root. This is what gets handed to ``SyncManager``."""
    return Path(output_dir) / get_display_name(group)


def sync_metadata_path(output_dir: Path, group: Group) -> Path:
    return service_root(output_dir, group) / "sync_metadata.json"


def sync_state_path(output_dir: Path, group: Group) -> Path:
    return service_root(output_dir, group) / "sync_state.json"


def messages_root(output_dir: Path, group: Group) -> Path:
    return service_root(output_dir, group) / "messages"


def blogs_root(output_dir: Path, group: Group) -> Path:
    return service_root(output_dir, group) / "blogs"


def blog_index_path(output_dir: Path, group: Group) -> Path:
    return blogs_root(output_dir, group) / "index.json"


def member_thumbnails_path(output_dir: Path, group: Group) -> Path:
    return blogs_root(output_dir, group) / "member_thumbnails"


def members_cache_path(output_dir: Path, group: Group) -> Path:
    return member_thumbnails_path(output_dir, group) / "members_cache.json"


def sanitize_member_dir_name(member_name: str) -> str:
    """
    Directory name for a blog member.

    Matches SakaDesk's ``BlogService.get_blog_cache_path``: only a trailing
    whitespace strip (Windows cannot create such directories) and a path
    separator replacement. Anything more aggressive would put the CLI's blogs
    in different folders than the GUI's.
    """
    return member_name.strip().replace("/", "_")


def blog_cache_path(
    output_dir: Path, group: Group, member_name: str, blog_id: str, date: str
) -> Path:
    """
    Directory holding one blog post's ``blog.json`` and ``images/``.

    Args:
        date: Publication date as ``YYYYMMDD`` (see :func:`blog_date_key`).
    """
    base = blogs_root(output_dir, group)
    return base / sanitize_member_dir_name(member_name) / f"{date}_{blog_id}"


def blog_date_key(published_at: str) -> str:
    """
    Derive the ``YYYYMMDD`` folder prefix from an ISO published_at string.

    Mirrors SakaDesk's ``build_download_queue``: ``published_at[:10]`` with
    dashes stripped, so ``2026-03-14T09:00:00+09:00`` becomes ``20260314``.
    """
    return published_at[:10].replace("-", "")
