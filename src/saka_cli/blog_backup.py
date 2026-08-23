"""
Blog backup that writes the same files SakaDesk's blog sync writes.

Two-stage design ported from SakaDesk's ``backend/services/blog_service.py``:

- Stage 1 (:func:`sync_blog_metadata`) parses list pages only and builds
  ``blogs/index.json``. Fast, and it is the single source of truth for a
  post's title/date/url.
- Stage 2 (:func:`download_blog_content`) fetches each post's detail page and
  writes ``blog.json`` plus ``images/``.

``member_thumbnails/`` is populated separately by
:func:`sync_member_thumbnails`, using the same content-hash cache the GUI uses
so repeat runs don't re-download every portrait.

Anything written here must stay byte-compatible with the GUI, since both
tools read and write the same tree.
"""

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import aiofiles
import aiohttp
import structlog
from pysaka import Group
from pysaka.blog import MAX_PAGES_SAFETY_CAP, BlogGoneError, get_scraper

from saka_cli import paths

logger = structlog.get_logger(__name__)

# Groups with an official blog. Yodel has none.
BLOG_SUPPORTED_GROUPS = frozenset(
    {Group.HINATAZAKA46, Group.NOGIZAKA46, Group.SAKURAZAKA46}
)

# Incremental runs only walk the newest few list pages; a first run walks
# everything. Same values the GUI uses.
INCREMENTAL_MAX_PAGES = 5

# Bounds copied from the GUI so the CLI puts the same load on the sites.
MEMBER_SCAN_CONCURRENCY = 5
BLOG_DOWNLOAD_CONCURRENCY = 5
IMAGE_DOWNLOAD_CONCURRENCY = 10


class BlogDownloadItem(NamedTuple):
    """One queued post. Metadata comes from the index, not the detail page."""

    blog_id: str
    member_id: str
    member_name: str
    title: str
    published_at: str
    url: str
    cache_path: Path


def is_blog_supported(group: Group) -> bool:
    return group in BLOG_SUPPORTED_GROUPS


def _build_blog_content(
    blog_id: str,
    member_name: str,
    title: str,
    published_at: str,
    url: str,
    html: str,
    images: list[dict],
) -> dict:
    """
    Build the ``blog.json`` payload.

    Metadata (title, published_at) comes from the index — the single source of
    truth. HTML content and images come from the scraper detail fetch. The
    shape has to match SakaDesk's ``_build_blog_content`` exactly.
    """
    return {
        "meta": {
            "id": blog_id,
            "member_name": member_name,
            "title": title,
            "published_at": published_at,
            "url": url,
        },
        "content": {
            "html": html,
        },
        "images": images,
    }


async def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via temp file + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    try:
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp_path, str(path))
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


# =============================================================================
# Index
# =============================================================================


async def load_blog_index(output_dir: Path, group: Group) -> dict:
    """Load ``blogs/index.json``, or an empty skeleton if absent/corrupt."""
    index_path = paths.blog_index_path(output_dir, group)
    if index_path.exists():
        try:
            async with aiofiles.open(index_path, encoding="utf-8") as f:
                return dict(json.loads(await f.read()))
        except Exception as e:
            logger.warning("Failed to load blog index; starting fresh", error=str(e))
    return {"members": {}, "last_sync": None, "last_download": None}


async def save_blog_index(output_dir: Path, group: Group, index: dict) -> None:
    await _atomic_write_json(paths.blog_index_path(output_dir, group), index)


# =============================================================================
# Member thumbnails
# =============================================================================


def _compute_members_hash(members: list[dict]) -> str:
    """Hash the whole member list — portraits are refreshed as a set."""
    sorted_members = sorted(members, key=lambda m: m["id"])
    return hashlib.sha256(
        json.dumps(sorted_members, sort_keys=True).encode()
    ).hexdigest()[:16]


async def _load_members_cache(output_dir: Path, group: Group) -> dict | None:
    cache_path = paths.members_cache_path(output_dir, group)
    if cache_path.exists():
        try:
            async with aiofiles.open(cache_path, encoding="utf-8") as f:
                return dict(json.loads(await f.read()))
        except Exception as e:
            logger.warning("Failed to load members cache", error=str(e))
    return None


async def _download_member_thumbnail(
    session: aiohttp.ClientSession,
    member_id: str,
    thumbnail_url: str,
    thumbnails_dir: Path,
) -> str | None:
    """Download one portrait as ``{member_id}{ext}``; return the filename."""
    try:
        async with session.get(thumbnail_url) as resp:
            if resp.status != 200:
                logger.warning(
                    "thumbnail_download_failed",
                    member_id=member_id,
                    status=resp.status,
                )
                return None

            content_type = resp.headers.get("Content-Type", "")
            if "jpeg" in content_type or "jpg" in content_type:
                ext = ".jpg"
            elif "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            else:
                ext = Path(thumbnail_url.split("?")[0]).suffix or ".jpg"

            local_filename = f"{member_id}{ext}"
            content = await resp.read()
            async with aiofiles.open(thumbnails_dir / local_filename, "wb") as f:
                await f.write(content)
            return local_filename
    except Exception as e:
        logger.warning("thumbnail_download_error", member_id=member_id, error=str(e))
        return None


async def sync_member_thumbnails(
    session: aiohttp.ClientSession, output_dir: Path, group: Group
) -> list[dict]:
    """
    Populate ``blogs/member_thumbnails/`` and its ``members_cache.json``.

    Skips the downloads entirely when the fetched member list hashes to the
    same value as the cache, mirroring the GUI's behaviour.
    """
    if not is_blog_supported(group):
        return []

    scraper = get_scraper(group, session)
    try:
        fresh_members = await scraper.get_members_with_thumbnails()
    except Exception as e:
        logger.warning("member_thumbnails_fetch_failed", error=str(e))
        fresh_members = []

    if not fresh_members:
        cache = await _load_members_cache(output_dir, group)
        return list(cache.get("members", [])) if cache else []

    fresh_data = [
        {"id": m.id, "name": m.name, "thumbnail_url": m.thumbnail_url}
        for m in fresh_members
    ]
    fresh_hash = _compute_members_hash(fresh_data)

    cache = await _load_members_cache(output_dir, group)
    if cache and cache.get("hash") == fresh_hash:
        logger.debug("members_cache_hit")
        return list(cache.get("members", []))

    thumbnails_dir = paths.member_thumbnails_path(output_dir, group)
    thumbnails_dir.mkdir(parents=True, exist_ok=True)

    result_members: list[dict] = []
    sem = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)

    async def download_and_add(member) -> None:
        async with sem:
            filename = await _download_member_thumbnail(
                session, member.id, member.thumbnail_url, thumbnails_dir
            )
        result_members.append(
            {"id": member.id, "name": member.name, "thumbnail": filename}
        )

    await asyncio.gather(*[download_and_add(m) for m in fresh_members])
    result_members.sort(key=lambda m: m["id"])

    await _atomic_write_json(
        paths.members_cache_path(output_dir, group),
        {"hash": fresh_hash, "members": result_members},
    )
    logger.info("members_thumbnails_cached", count=len(result_members))
    return result_members


# =============================================================================
# Stage 1: metadata -> index.json
# =============================================================================


async def sync_blog_metadata(
    session: aiohttp.ClientSession,
    output_dir: Path,
    group: Group,
    member_ids: Iterable[str] | None = None,
    on_member_done: Callable[[str, int], None] | None = None,
) -> dict:
    """
    Stage 1: index every member's posts into ``blogs/index.json``.

    Args:
        member_ids: Restrict the scan to these members. The index still keeps
            entries for everyone scanned previously — a filtered run adds to
            the index rather than replacing it.
        on_member_done: Called with (member_name, new_post_count) as each
            member finishes, for progress display.

    Returns:
        The updated index dict.
    """
    if not is_blog_supported(group):
        logger.info("Blogs not supported for this group", group=group.value)
        return {"members": {}, "last_sync": None, "last_download": None}

    index = await load_blog_index(output_dir, group)
    index.setdefault("members", {})

    # A first run walks the full archive; later runs only the newest pages.
    is_first_sync = not index["members"]
    max_pages = MAX_PAGES_SAFETY_CAP if is_first_sync else INCREMENTAL_MAX_PAGES

    scraper = get_scraper(group, session)
    all_members = await scraper.get_members()

    if member_ids:
        wanted = {str(m) for m in member_ids}
        targets = {k: v for k, v in all_members.items() if str(k) in wanted}
    else:
        targets = dict(all_members)

    member_sem = asyncio.Semaphore(MEMBER_SCAN_CONCURRENCY)

    async def sync_member(member_id: str, member_name: str) -> None:
        if member_id not in index["members"]:
            index["members"][member_id] = {"name": member_name, "blogs": []}
        else:
            index["members"][member_id]["name"] = member_name

        existing_ids = {b["id"] for b in index["members"][member_id]["blogs"]}

        # Incremental: stop paginating once we reach posts we already have.
        since_date = None
        if not is_first_sync and index["members"][member_id]["blogs"]:
            newest = max(
                b["published_at"] for b in index["members"][member_id]["blogs"]
            )
            with suppress(ValueError, TypeError):
                since_date = datetime.fromisoformat(newest)

        new_entries: list[dict] = []
        needs_detail: list[dict] = []

        async with member_sem:
            async for entry in scraper.get_blogs_metadata(
                member_id,
                since_date=since_date,
                max_pages=max_pages,
                member_name=member_name,
            ):
                if entry.id in existing_ids:
                    continue
                blog_data = {
                    "id": entry.id,
                    "title": entry.title,
                    "published_at": entry.published_at,
                    "url": entry.url,
                    "thumbnail": entry.images[0] if entry.images else None,
                }
                new_entries.append(blog_data)
                # Sakurazaka list pages omit the time and thumbnail and can
                # truncate titles, so the detail page is authoritative there.
                if not blog_data["thumbnail"]:
                    needs_detail.append(blog_data)

            if needs_detail:

                async def fetch_detail(blog: dict) -> None:
                    try:
                        thumb, date, title = await scraper.get_blog_detail_metadata(
                            blog["id"]
                        )
                        if thumb:
                            blog["thumbnail"] = thumb
                        if date:
                            blog["published_at"] = date
                        if title:
                            blog["title"] = title
                    except Exception as e:
                        logger.debug(
                            "detail_metadata_failed", blog_id=blog["id"], error=str(e)
                        )

                await asyncio.gather(*[fetch_detail(b) for b in needs_detail])

        for blog_data in new_entries:
            pub_at = blog_data["published_at"]
            index["members"][member_id]["blogs"].append(
                {
                    "id": blog_data["id"],
                    "title": blog_data["title"],
                    "published_at": pub_at.isoformat()
                    if hasattr(pub_at, "isoformat")
                    else pub_at,
                    "url": blog_data["url"],
                    "thumbnail": blog_data["thumbnail"],
                }
            )

        if on_member_done:
            on_member_done(member_name, len(new_entries))

    # return_exceptions so one member's scrape failure doesn't discard every
    # other member's freshly scanned metadata before the save below.
    results = await asyncio.gather(
        *[sync_member(mid, name) for mid, name in targets.items()],
        return_exceptions=True,
    )
    for member_id, res in zip(targets.keys(), results):
        if isinstance(res, Exception):
            logger.warning(
                "blog_metadata_member_sync_failed",
                member_id=member_id,
                error_type=type(res).__name__,
                error=str(res),
            )

    index["last_sync"] = datetime.now(timezone.utc).isoformat()
    await save_blog_index(output_dir, group, index)
    return index


# =============================================================================
# Stage 2: content -> blog.json + images/
# =============================================================================


def build_download_queue(
    output_dir: Path,
    group: Group,
    index: dict,
    member_ids: Iterable[str] | None = None,
    skip_cached: bool = True,
) -> list[BlogDownloadItem]:
    """Queue every indexed post that has no ``blog.json`` on disk yet."""
    wanted = {str(m) for m in member_ids} if member_ids else None
    queue: list[BlogDownloadItem] = []

    for member_id, member_data in index.get("members", {}).items():
        if wanted is not None and str(member_id) not in wanted:
            continue
        # Members whose blogs are all gone (e.g. graduated) stay in the index
        # but have nothing left to fetch.
        if member_data.get("blogs_removed"):
            continue

        member_name = member_data.get("name", "")

        for blog in member_data.get("blogs", []):
            if blog.get("removed"):
                continue

            cache_path = paths.blog_cache_path(
                output_dir,
                group,
                member_name,
                blog["id"],
                paths.blog_date_key(blog["published_at"]),
            )
            if skip_cached and (cache_path / "blog.json").exists():
                continue

            queue.append(
                BlogDownloadItem(
                    blog_id=blog["id"],
                    member_id=member_id,
                    member_name=member_name,
                    title=blog["title"],
                    published_at=blog["published_at"],
                    url=blog["url"],
                    cache_path=cache_path,
                )
            )

    return queue


async def _download_images(
    session: aiohttp.ClientSession,
    image_urls: list[str],
    images_dir: Path,
    semaphore: asyncio.Semaphore | None = None,
) -> list[dict]:
    """
    Download a post's images into ``images/`` as ``img_{idx}{ext}``.

    Every slot in the returned list is filled even on failure — a ``None``
    entry serialized into blog.json would break the GUI's image rewriting.
    """
    results: list[dict] = [
        {"original_url": u, "local_path": None} for u in image_urls
    ]

    async def download_image(idx: int, img_url: str) -> None:
        ext = Path(img_url).suffix or ".jpg"
        if "?" in ext:
            ext = ext.split("?")[0]
        local_name = f"img_{idx}{ext}"
        local_path = images_dir / local_name

        async def _fetch() -> None:
            async with session.get(img_url) as resp:
                if resp.status == 200:
                    images_dir.mkdir(parents=True, exist_ok=True)
                    content = await resp.read()
                    async with aiofiles.open(local_path, "wb") as f:
                        await f.write(content)
                    results[idx] = {
                        "original_url": img_url,
                        "local_path": f"./images/{local_name}",
                    }
                else:
                    logger.debug(
                        "Image download non-200", url=img_url, status=resp.status
                    )
                    results[idx] = {"original_url": img_url, "local_path": None}

        try:
            # Acquire before session.get so the aiohttp timeout clock doesn't
            # start while the request is still queued behind the semaphore.
            if semaphore:
                async with semaphore:
                    await _fetch()
            else:
                await _fetch()
        except Exception as e:
            logger.warning(
                "Failed to download image",
                url=img_url,
                error_type=type(e).__name__,
                error=str(e) or "(no message)",
            )
            results[idx] = {"original_url": img_url, "local_path": None}

    await asyncio.gather(*[download_image(i, u) for i, u in enumerate(image_urls)])
    return results


def render_blog_html(content: dict) -> str:
    """
    Render a standalone ``index.html`` from a ``blog.json`` payload.

    CLI-only convenience for offline reading (``--html``); the GUI never
    writes or reads this file. Local image paths are already relative to the
    post directory, so the page works straight off disk.
    """
    meta = content["meta"]
    html = content["content"]["html"]

    for img in content.get("images", []):
        local = img.get("local_path")
        if local:
            html = html.replace(img["original_url"], local)

    published = meta.get("published_at") or ""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>{meta.get("title", "")}</title>
<style>
body {{ font-family: sans-serif; max-width: 800px; margin: 20px auto; padding: 0 20px; line-height: 1.6; }}
img {{ max-width: 100%; height: auto; }}
.date {{ color: #666; font-size: 0.9em; }}
h1 {{ margin-bottom: 0.5em; }}
</style>
</head>
<body>
<h1>{meta.get("title", "")}</h1>
<div class="date">{published}</div>
<hr>
{html}
</body>
</html>"""


async def _download_single_blog(
    session: aiohttp.ClientSession,
    scraper,
    item: BlogDownloadItem,
    download_images: bool,
    image_sem: asyncio.Semaphore | None,
    write_html: bool,
) -> None:
    """Fetch one post's detail page and write blog.json (+ images, + html)."""
    entry = await scraper.get_blog_detail(item.blog_id, member_id=item.member_id)

    item.cache_path.mkdir(parents=True, exist_ok=True)

    if download_images and entry.images:
        images_result = await _download_images(
            session, entry.images, item.cache_path / "images", image_sem
        )
    else:
        images_result = [
            {"original_url": img, "local_path": None} for img in entry.images
        ]

    content = _build_blog_content(
        blog_id=item.blog_id,
        member_name=item.member_name,
        title=item.title,
        published_at=item.published_at,
        url=item.url,
        html=entry.content,
        images=images_result,
    )

    await _atomic_write_json(item.cache_path / "blog.json", content)

    if write_html:
        async with aiofiles.open(
            item.cache_path / "index.html", "w", encoding="utf-8"
        ) as f:
            await f.write(render_blog_html(content))


async def download_blog_content(
    session: aiohttp.ClientSession,
    output_dir: Path,
    group: Group,
    queue: list[BlogDownloadItem],
    index: dict | None = None,
    download_images: bool = True,
    write_html: bool = False,
    on_item_done: Callable[[], None] | None = None,
) -> dict[str, int]:
    """
    Stage 2: download every queued post.

    Returns a ``{"downloaded", "failed"}`` count. When ``index`` is given its
    ``last_download`` stamp is refreshed and it is written back to disk.
    """
    stats = {"total": len(queue), "downloaded": 0, "removed": 0, "failed": 0}
    if not queue:
        return stats

    scraper = get_scraper(group, session)
    blog_sem = asyncio.Semaphore(BLOG_DOWNLOAD_CONCURRENCY)
    image_sem = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)

    async def run(item: BlogDownloadItem) -> None:
        async with blog_sem:
            try:
                await _download_single_blog(
                    session, scraper, item, download_images, image_sem, write_html
                )
                stats["downloaded"] += 1
            except BlogGoneError:
                # Permanently removed upstream (404/410). Flag it so future
                # runs skip it instead of retrying forever.
                if index is not None:
                    _mark_blog_removed(index, item.member_id, item.blog_id)
                stats["removed"] += 1
            except Exception as e:
                stats["failed"] += 1
                logger.warning(
                    "blog_download_failed",
                    blog_id=item.blog_id,
                    member=item.member_name,
                    error_type=type(e).__name__,
                    error=str(e),
                )
            finally:
                if on_item_done:
                    on_item_done()

    await asyncio.gather(*[run(item) for item in queue])

    if index is not None:
        _promote_fully_removed_members(index)
        index["last_download"] = datetime.now(timezone.utc).isoformat()
        await save_blog_index(output_dir, group, index)

    logger.info("blog_download_complete", **stats)
    return stats


def _mark_blog_removed(index: dict, member_id: str, blog_id: str) -> None:
    """Flag one post as permanently removed in the index."""
    member_data = index.get("members", {}).get(member_id, {})
    for blog in member_data.get("blogs", []):
        if blog["id"] == blog_id:
            blog["removed"] = True
            break


def _promote_fully_removed_members(index: dict) -> None:
    """
    Set the member-level ``blogs_removed`` flag once every post is gone, so
    later runs skip the member instead of walking hundreds of dead entries.
    """
    for member_id, member_data in index.get("members", {}).items():
        if member_data.get("blogs_removed"):
            continue
        blogs = member_data.get("blogs", [])
        if blogs and all(b.get("removed") for b in blogs):
            member_data["blogs_removed"] = True
            logger.info(
                "All blogs removed for member",
                member_id=member_id,
                member_name=member_data.get("name"),
                blog_count=len(blogs),
            )
