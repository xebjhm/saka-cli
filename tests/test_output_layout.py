"""
End-to-end checks that the CLI writes the tree SakaDesk's sync writes.

These drive the real pipeline (pysaka's SyncManager, the real blog stages)
against fake network layers and assert the resulting paths and file schemas.
The expected values here are transcribed from SakaDesk's
``backend/services/path_resolver.py``, ``sync_service.py`` and
``blog_service.py`` — if the GUI's layout moves, these fail.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pysaka import Group, SyncManager

from saka_cli import blog_backup, message_sync, paths
from saka_cli import metadata as meta_store

DISPLAY = "日向坂46"
GROUP = Group.HINATAZAKA46

GROUPS = [
    {"id": 1, "name": "1st Single", "state": "open", "thumbnail": "g1.jpg",
     "subscription": {"state": "active"}, "unread_count": 3},
    {"id": 2, "name": "Closed Single", "state": "closed",
     "subscription": {"state": "cancelled"}},
]

MEMBERS = {
    1: [
        {"id": 40, "name": "松田 好花", "thumbnail": "t40.jpg", "portrait": "p40.jpg"},
        {"id": 41, "name": "上村 ひなの", "thumbnail": "t41.jpg", "portrait": "p41.jpg"},
    ],
}

MESSAGES = [
    {"id": 1001, "member_id": 40, "text": "hello", "type": "text",
     "published_at": "2026-03-14T09:00:00Z"},
    {"id": 1002, "member_id": 40, "file": "https://cdn.example/x.jpg",
     "type": "picture", "published_at": "2026-03-14T10:00:00Z"},
    {"id": 1003, "member_id": 41, "text": "hi", "type": "text",
     "published_at": "2026-03-14T11:00:00Z"},
]


class FakeClient:
    """Stands in for pysaka's Client; records what the pipeline asked for."""

    def __init__(self):
        self.downloaded = []

    async def get_groups(self, session, include_inactive=False):
        # Mirrors pysaka's Client.get_groups: closed groups are ALWAYS
        # returned (for graduation detection) regardless of include_inactive;
        # open groups need an active subscription unless include_inactive.
        out = []
        for g in GROUPS:
            if g.get("state") == "closed":
                out.append(g)
                continue
            sub_state = (g.get("subscription") or {}).get("state")
            if sub_state == "active" or (include_inactive and sub_state is not None):
                out.append(g)
        return out

    async def get_members(self, session, group_id):
        return MEMBERS.get(group_id, [])

    async def get_messages(self, session, group_id, since_ts=None):
        return list(MESSAGES) if group_id == 1 else []

    async def download_file(self, session, url, path, timestamp):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-image-bytes")
        self.downloaded.append(path)
        return True


@pytest.fixture
def synced(tmp_path):
    """Run a full message sync into tmp_path and return (out_dir, client)."""
    import asyncio

    out = tmp_path / "output"
    client = FakeClient()
    service_root = paths.service_root(out, GROUP)
    service_root.mkdir(parents=True, exist_ok=True)
    manager = SyncManager(client, service_root)

    async def run():
        metadata = await meta_store.load_metadata(
            paths.sync_metadata_path(out, GROUP)
        )
        tasks = await message_sync.discover_tasks(None, client, metadata)
        media_queue = []
        await message_sync.sync_messages(
            None, manager, tasks, media_queue, metadata
        )
        await message_sync.download_media(None, manager, media_queue)
        meta_store.stamp_last_sync(metadata)
        await meta_store.save_metadata(
            paths.sync_metadata_path(out, GROUP), metadata
        )

    asyncio.run(run())
    return out, client


# =============================================================================
# Message tree
# =============================================================================


def test_message_tree_matches_gui_layout(synced):
    """Messages land at {out}/日向坂46/messages/{gid} {group}/{mid} {member}/."""
    out, _ = synced
    member_dir = out / DISPLAY / "messages" / "1 1st Single" / "40 松田 好花"

    assert member_dir.is_dir()
    assert (member_dir / "messages.json").is_file()
    for sub in ("picture", "video", "voice"):
        assert (member_dir / sub).is_dir(), f"missing {sub}/"

    assert (out / DISPLAY / "messages" / "1 1st Single" / "41 上村 ひなの").is_dir()


def test_state_files_live_under_the_service_root(synced):
    """sync_state.json and sync_metadata.json are per-service, not per-output."""
    out, _ = synced
    assert (out / DISPLAY / "sync_state.json").is_file()
    assert (out / DISPLAY / "sync_metadata.json").is_file()
    # Nothing may be written at the bare output root — that was the old bug.
    assert not (out / "messages").exists()
    assert not (out / "sync_state.json").exists()


def test_closed_groups_are_skipped(synced):
    """Closed groups 404 on members/timeline, so they get no directory."""
    out, _ = synced
    messages_root = out / DISPLAY / "messages"
    assert [p.name for p in messages_root.iterdir()] == ["1 1st Single"]


def test_media_is_stored_by_message_id_under_its_type(synced):
    out, client = synced
    picture = (
        out / DISPLAY / "messages" / "1 1st Single" / "40 松田 好花"
        / "picture" / "1002.jpg"
    )
    assert picture.is_file()
    assert client.downloaded == [picture]


def test_messages_json_media_path_is_relative_to_service_root(synced):
    """The GUI resolves media_file against the service root, not the cwd."""
    out, _ = synced
    data = json.loads(
        (out / DISPLAY / "messages" / "1 1st Single" / "40 松田 好花" / "messages.json")
        .read_text(encoding="utf-8")
    )
    media_msg = next(m for m in data["messages"] if m["id"] == 1002)
    assert media_msg["media_file"] == str(Path("messages/1 1st Single/40 松田 好花/picture/1002.jpg"))


# =============================================================================
# sync_metadata.json
# =============================================================================


def test_sync_metadata_schema(synced):
    """Schema must match what SakaDesk's load_sync_metadata expects."""
    out, _ = synced
    meta = json.loads(
        (out / DISPLAY / "sync_metadata.json").read_text(encoding="utf-8")
    )

    assert set(meta) >= {"groups", "server_groups", "last_sync"}
    datetime.fromisoformat(meta["last_sync"])  # parseable ISO timestamp

    entry = meta["groups"]["1_40"]
    assert entry["group_id"] == 1
    assert entry["group_name"] == "1st Single"
    assert entry["group_thumbnail"] == "g1.jpg"
    assert entry["member_id"] == 40
    assert entry["member_name"] == "松田 好花"
    assert entry["thumbnail"] == "t40.jpg"
    assert entry["portrait"] == "p40.jpg"
    assert entry["last_message_id"] == 1002


def test_sync_metadata_records_server_group_state(synced):
    out, _ = synced
    meta = json.loads(
        (out / DISPLAY / "sync_metadata.json").read_text(encoding="utf-8")
    )

    assert meta["server_groups"]["1"] == {
        "state": "open",
        "is_active": True,
        "unread_count": 3,
    }
    # A closed group is never active, whatever its subscription says.
    assert meta["server_groups"]["2"]["is_active"] is False
    # unread_count is omitted by the API when zero.
    assert meta["server_groups"]["2"]["unread_count"] == 0


# =============================================================================
# Blogs
# =============================================================================


class FakeEntry:
    def __init__(self, blog_id, title, published_at, images=(), content="<p>body</p>"):
        self.id = blog_id
        self.title = title
        self.published_at = published_at
        self.url = f"https://example.com/blog/{blog_id}"
        self.images = list(images)
        self.content = content
        self.member_id = "40"
        self.member_name = "松田 好花"


class FakeScraper:
    def __init__(self, session):
        self.session = session

    async def get_members(self):
        return {"40": "松田 好花"}

    async def get_members_with_thumbnails(self):
        return []

    async def get_blogs_metadata(self, member_id, since_date=None, max_pages=3, member_name=None):
        yield FakeEntry(
            "555",
            "こんばんは",
            datetime(2026, 3, 14, 21, 0, tzinfo=timezone.utc),
            images=["https://cdn.example/pic.jpg"],
        )

    async def get_blog_detail(self, blog_id, member_id=None):
        return FakeEntry(
            blog_id,
            "こんばんは",
            datetime(2026, 3, 14, 21, 0, tzinfo=timezone.utc),
            images=["https://cdn.example/pic.jpg"],
            content='<p><img src="https://cdn.example/pic.jpg"></p>',
        )


class FakeResponse:
    status = 200
    headers = {"Content-Type": "image/jpeg"}

    async def read(self):
        return b"fake-image-bytes"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def get(self, url):
        return FakeResponse()


@pytest.fixture
def blogged(tmp_path, monkeypatch):
    """Run both blog stages into tmp_path and return the output dir."""
    import asyncio

    monkeypatch.setattr(blog_backup, "get_scraper", lambda g, s: FakeScraper(s))
    out = tmp_path / "output"
    session = FakeSession()

    async def run():
        index = await blog_backup.sync_blog_metadata(session, out, GROUP)
        queue = blog_backup.build_download_queue(out, GROUP, index)
        await blog_backup.download_blog_content(
            session, out, GROUP, queue, index=index
        )

    asyncio.run(run())
    return out


def test_blog_tree_matches_gui_layout(blogged):
    """Posts land at blogs/{member}/{YYYYMMDD}_{id}/ with blog.json + images/."""
    post_dir = blogged / DISPLAY / "blogs" / "松田 好花" / "20260314_555"
    assert (post_dir / "blog.json").is_file()
    assert (post_dir / "images" / "img_0.jpg").is_file()
    # index.html is opt-in (--html) and must not appear by default.
    assert not (post_dir / "index.html").exists()


def test_blog_json_schema(blogged):
    """Schema must match SakaDesk's _build_blog_content exactly."""
    post_dir = blogged / DISPLAY / "blogs" / "松田 好花" / "20260314_555"
    data = json.loads((post_dir / "blog.json").read_text(encoding="utf-8"))

    assert set(data) == {"meta", "content", "images"}
    assert set(data["meta"]) == {"id", "member_name", "title", "published_at", "url"}
    assert set(data["content"]) == {"html"}
    assert data["meta"]["id"] == "555"
    assert data["meta"]["member_name"] == "松田 好花"
    assert data["images"] == [
        {"original_url": "https://cdn.example/pic.jpg", "local_path": "./images/img_0.jpg"}
    ]


def test_blog_index_schema(blogged):
    index = json.loads(
        (blogged / DISPLAY / "blogs" / "index.json").read_text(encoding="utf-8")
    )

    assert set(index) >= {"members", "last_sync", "last_download"}
    member = index["members"]["40"]
    assert member["name"] == "松田 好花"
    blog = member["blogs"][0]
    assert set(blog) == {"id", "title", "published_at", "url", "thumbnail"}
    # published_at is serialized as ISO, not a datetime repr.
    datetime.fromisoformat(blog["published_at"])


def test_second_run_skips_cached_posts(blogged, monkeypatch):
    """A post with blog.json on disk is not re-queued."""
    import asyncio

    monkeypatch.setattr(blog_backup, "get_scraper", lambda g, s: FakeScraper(s))
    index = asyncio.run(blog_backup.load_blog_index(blogged, GROUP))
    assert blog_backup.build_download_queue(blogged, GROUP, index) == []


def test_html_is_written_when_requested(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(blog_backup, "get_scraper", lambda g, s: FakeScraper(s))
    out = tmp_path / "output"
    session = FakeSession()

    async def run():
        index = await blog_backup.sync_blog_metadata(session, out, GROUP)
        queue = blog_backup.build_download_queue(out, GROUP, index)
        await blog_backup.download_blog_content(
            session, out, GROUP, queue, index=index, write_html=True
        )

    asyncio.run(run())

    post_dir = out / DISPLAY / "blogs" / "松田 好花" / "20260314_555"
    html = (post_dir / "index.html").read_text(encoding="utf-8")
    # The remote URL is rewritten to the on-disk copy so the page works offline.
    assert "./images/img_0.jpg" in html
    assert "https://cdn.example/pic.jpg" not in html
    # blog.json keeps the original URL alongside the local path.
    data = json.loads((post_dir / "blog.json").read_text(encoding="utf-8"))
    assert data["images"][0]["original_url"] == "https://cdn.example/pic.jpg"


def test_yodel_has_no_blogs():
    assert not blog_backup.is_blog_supported(Group.YODEL)
    for g in (Group.HINATAZAKA46, Group.NOGIZAKA46, Group.SAKURAZAKA46):
        assert blog_backup.is_blog_supported(g)


# =============================================================================
# ID parsing
# =============================================================================


def test_blog_member_ids_survive_parsing():
    """Blog member IDs are site codes, not numbers — parsing must not coerce.

    Nogizaka's codes are zero-padded, so int() round-tripping would turn
    "40006" fine but "00123" into "123" and never match the scraper's key.
    """
    from saka_cli.cli import parse_id_list

    assert parse_id_list("00123,40006") == ["00123", "40006"]
    assert parse_id_list("40006 40007") == ["40006", "40007"]
    assert parse_id_list("40, 41 ,42") == ["40", "41", "42"]
    assert parse_id_list("") == []


def test_download_queue_filters_by_string_member_id(tmp_path):
    index = {
        "members": {
            "00123": {"name": "A", "blogs": [
                {"id": "1", "title": "t", "published_at": "2026-01-01T00:00:00+09:00", "url": "u"}]},
            "40006": {"name": "B", "blogs": [
                {"id": "2", "title": "t", "published_at": "2026-01-02T00:00:00+09:00", "url": "u"}]},
        }
    }
    q = blog_backup.build_download_queue(
        tmp_path, GROUP, index, member_ids=["00123"], skip_cached=False
    )
    assert [i.member_id for i in q] == ["00123"]
