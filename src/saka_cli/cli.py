import asyncio
import argparse
import json
import traceback
import sys
import structlog
from pathlib import Path
from typing import Optional
from tqdm import tqdm
from datetime import datetime, timezone
import locale
from pysaka import (
    Client,
    BrowserAuth,
    SyncManager,
    Group,
    get_auth_dir,
    get_user_data_dir,
    SessionExpiredError,
)

from saka_cli import blog_backup, message_sync, paths
from saka_cli import metadata as meta_store
from saka_cli.hardware import detect_hardware, suggest_llm_backend
from saka_cli.kb import run_kb_command
from saka_cli.logging_setup import setup_logging
from saka_cli.strings import get_string, set_language, get_language

# Logger created at import as a lazy structlog proxy (safe before configuration);
# main() rebinds it after setup_logging() so it picks up the final config.
logger = structlog.get_logger()


def detect_system_language():
    """Detect system language in a cross-platform way."""
    lang_code = "en"
    try:
        # Priority 1: Environment Variables (Standard override)
        import os

        lang_sys = (
            os.environ.get("LANG")
            or os.environ.get("LANGUAGE")
            or os.environ.get("LC_ALL")
        )

        # Priority 2: locale module (OS Default) - use getlocale() instead of deprecated getdefaultlocale()
        if not lang_sys:
            try:
                locale.setlocale(locale.LC_ALL, "")  # Initialize from environment
                lang_sys = locale.getlocale()[0]
            except Exception:
                pass

        if lang_sys:
            if "ja" in lang_sys.lower():
                lang_code = "ja"
            elif "zh" in lang_sys.lower():
                if "TW" in lang_sys or "HK" in lang_sys or "Hant" in lang_sys:
                    lang_code = "zh-TW"
                else:
                    lang_code = "zh-CN"
            # Cantonese check (rarely explicit in standard locales, usually zh-HK)
            elif "yue" in lang_sys.lower():
                lang_code = "yue"

    except Exception as e:
        if logger:
            logger.debug(f"Language detection failed: {e}")

    return lang_code


# CLI Config
DEFAULT_OUTPUT = "output"
DEFAULT_CONFIG = "config.json"
CLI_PREFS_FILE = "cli_preferences.json"


def get_cli_prefs_file() -> Path:
    """Get path to the global CLI preferences file.

    Stores settings that apply across all groups: language, output directory, etc.
    Location: {user_data_dir}/cli_preferences.json
    """
    return get_user_data_dir() / CLI_PREFS_FILE


def load_cli_prefs() -> dict:
    """Load global CLI preferences."""
    prefs_file = get_cli_prefs_file()
    if prefs_file.exists():
        with open(prefs_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cli_prefs(prefs: dict) -> None:
    """Save global CLI preferences."""
    prefs_file = get_cli_prefs_file()
    with open(prefs_file, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)


def parse_int_list(value: str) -> list[int]:
    """Parse comma or space-separated integers."""
    if "," in value:
        return [int(x.strip()) for x in value.split(",") if x.strip()]
    return [int(value)]


def parse_id_list(value: str) -> list[str]:
    """Parse comma or space-separated IDs, keeping them as strings.

    Blog member IDs are site codes, not numbers — Nogizaka's are zero-padded
    and are matched against the scraper's own string keys — so coercing them
    to int would either raise or silently change the ID.
    """
    parts = value.replace(",", " ").split()
    return [p.strip() for p in parts if p.strip()]


class SakaCLI:
    def __init__(
        self,
        output_dir: str = DEFAULT_OUTPUT,
        group: Group = Group.HINATAZAKA46,
        write_html: bool = False,
        initial_limit: int = message_sync.DEFAULT_INITIAL_MESSAGE_LIMIT,
    ):
        self.output_dir = Path(output_dir)
        self.group = group
        self.write_html = write_html
        self.initial_limit = initial_limit
        # Everything this run writes lives under {output_dir}/{display_name},
        # the same per-service root SakaDesk uses, so both tools can share one
        # output tree. See saka_cli.paths for the full layout.
        self.service_root = paths.service_root(self.output_dir, group)
        # Store config in user data directory for consistent location across platforms
        # Windows: %APPDATA%/pysaka, macOS: ~/Library/Application Support/pysaka, Linux: ~/.local/share/pysaka
        self.config_file = get_user_data_dir() / f"config_{group.value}.json"
        self.client = None
        self.manager = None

    def load_config(self) -> dict:
        if self.config_file.exists():
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_config(self, config: dict):
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def show_session_expired(self):
        """
        Display a user-friendly session expired message.
        Similar to the official app: clean, clear, and actionable.
        Uses a simple format that works reliably across all terminals.
        """
        line = "─" * 62

        print()
        print(f"  {line}")
        print()
        print(f"    {get_string('session_expired_title')}")
        print()

        # Handle multi-line message
        message = get_string("session_expired_message").replace("\\n", "\n")
        for msg_line in message.split("\n"):
            if msg_line:
                print(f"    {msg_line}")

        print()
        print(f"  {line}")
        print()
        input(get_string("session_expired_prompt"))

    def check_tos(self) -> bool:
        """
        Check if user agreed to ToS. If not, prompt and block.
        Returns True if agreed, False if declined.
        """
        config = self.load_config()
        if config.get("tos_agreed"):
            return True

        # Get link based on group value
        group_val = self.group.value
        if group_val == "sakurazaka46":
            link = "https://sakurazaka46.com/s/s46app/page/app_terms"
        elif group_val == "hinatazaka46":
            link = "https://www.hinatazaka46.com/s/h46app/page/app_terms"
        else:  # nogizaka46 (default)
            link = "https://contact.nogizaka46.com/s/n46app/page/app_terms"

        # Display ToS
        print(get_string("tos_title"))
        print(get_string("tos_warn").format(link))

        # Block
        try:
            choice = input(get_string("tos_prompt")).strip().lower()
        except EOFError:
            # Handle non-interactive environments that didn't pre-agree
            choice = "n"

        if choice == "y":
            config["tos_agreed"] = True
            self.save_config(config)
            return True
        else:
            print(get_string("tos_declined"))
            return False

    def run_interactive_wizard(self, force_lang=False):
        """
        Interactive wizard to gather configuration from the user.
        Returns a dictionary with configuration keys.
        """
        # USE PRINT FOR UI ELEMENTS
        print(get_string("interactive_title"))

        config = {}

        # -1. Language Selection (Skip if already strict via --lang, or auto-detected non-English)
        current_lang = get_language()
        if force_lang:
            # Explicitly forced by CLI arg, skip prompt
            selected_lang = current_lang
        elif current_lang != "en":
            # Language was already detected from config/system - use it silently
            selected_lang = current_lang
        else:
            # First-time or English default - show prompt
            print(get_string("interactive_lang"), end="")
            lang_choice = input().strip()
            selected_lang = "en"  # Default
            if lang_choice:
                if lang_choice == "2":
                    selected_lang = "ja"
                elif lang_choice == "3":
                    selected_lang = "zh-TW"
                elif lang_choice == "4":
                    selected_lang = "zh-CN"
                elif lang_choice == "5":
                    selected_lang = "yue"
            set_language(selected_lang)
            # Save language to global CLI preferences
            prefs = load_cli_prefs()
            prefs["lang"] = selected_lang
            save_cli_prefs(prefs)

        # 0. Service Selection
        print(get_string("interactive_service"), end="")
        service_choice = input().strip()
        if service_choice == "2":
            config["service"] = "sakurazaka46"
        elif service_choice == "3":
            config["service"] = "hinatazaka46"
        else:
            config["service"] = "nogizaka46"

        # [Fix] Check ToS immediately after service selection (before proceeding)
        # We need to check the config for the SELECTED service, not the default one.
        temp_group = Group(config["service"])
        temp_cli = SakaCLI(group=temp_group)
        if not temp_cli.check_tos():
            sys.exit(0)  # Exit if declined in wizard

        # 1. Output Directory
        user_out = input(get_string("interactive_out").format(DEFAULT_OUTPUT)).strip()
        config["output_dir"] = user_out if user_out else DEFAULT_OUTPUT

        # Mode Selection (all groups support blog backup)
        mode = "message"
        print(get_string("mode_selection"), end="")
        m_choice = input().strip()
        if m_choice == "2":
            mode = "blog"

        config["mode"] = mode

        if mode == "blog":
            # Blog Flow
            print(get_string("blog_fetching_members"))
            blog_cli = SakaCLI(group=Group(config["service"]))
            members = asyncio.run(blog_cli.fetch_blog_members())

            print(get_string("blog_select_members"))
            for m_id, m_name in members.items():
                print(f"[{m_id}] {m_name}")

            sel = input(get_string("blog_id_prompt")).strip()
            # Blog IDs stay strings — see parse_id_list.
            config["members"] = parse_id_list(sel) if sel else []

            # Cache members to avoid duplicate fetch in run_blog_backup
            config["_blog_members_cache"] = members

            print(get_string("interactive_end"))
            return config

        # 2. Offline Members
        user_off = input(get_string("interactive_offline")).strip().lower()
        config["include_offline"] = user_off == "y"

        # 3. Fetch and display subscribed members
        print(get_string("msg_fetching_members"))
        msg_cli = SakaCLI(group=Group(config["service"]))
        try:
            members = asyncio.run(msg_cli.fetch_msg_members(config["include_offline"]))
        except SessionExpiredError:
            print(get_string("session_expired_prompt"))
            try:
                # Reuse the robust setup wizard for login (handles Chrome install, config, etc.)
                asyncio.run(msg_cli.setup_wizard())

                # Re-initialize client to pick up the newly saved credentials from storage
                # This ensures we have a clean state with the fresh token/cookies
                msg_cli = SakaCLI(group=Group(config["service"]))

                # Retry fetching members
                members = asyncio.run(
                    msg_cli.fetch_msg_members(config["include_offline"])
                )
            except Exception as e:
                logger.error(f"Login failed: {e}")
                members = []

        if not members:
            logger.warning(get_string("msg_no_members"))
            print(get_string("interactive_end"))
            return config

        # Split online and offline members for clearer display
        online_members = [m for m in members if m.get("state") == "active"]
        offline_members = [m for m in members if m.get("state") != "active"]

        if online_members:
            print(get_string("msg_select_members"))
            for m in online_members:
                print(f"[{m['id']}] {m['name']}")

        if offline_members:
            print(get_string("msg_offline_members"))
            for m in offline_members:
                print(f"[{m['id']}] {m['name']}")

        sel = input(get_string("msg_id_prompt")).strip()
        if sel:
            try:
                config["members"] = [int(x) for x in sel.replace(",", " ").split()]
            except ValueError:
                logger.warning("Invalid member IDs, selecting all.")
                config["members"] = []
        else:
            config["members"] = []

        print(get_string("interactive_end"))
        return config

    async def cleanup_wizard(self):
        import platform

        is_linux = platform.system() == "Linux"

        # UI Header (Print is fine/required for Wizard interactions)
        print(get_string("cleanup_title"))
        print(get_string("cleanup_warn"))
        if is_linux:
            print(get_string("cleanup_linux_chrome"))

        confirm = input(get_string("cleanup_confirm"))
        if confirm.lower() != "y":
            logger.info(get_string("cleanup_aborted"))
            return

        logger.info(get_string("cleanup_removing"))
        import shutil

        # Remove auth_data from userData path
        auth_path = get_auth_dir()
        if auth_path.exists():
            try:
                shutil.rmtree(auth_path)
                logger.info(get_string("cleanup_removed").format(auth_path))
            except Exception as e:
                logger.error(get_string("cleanup_fail").format(auth_path, e))

        # Remove ALL keys from Keyring
        try:
            from pysaka.credentials import TokenManager
            from pysaka.client import Group

            cleaned_tokens = 0
            # Iterate over all defined groups in the generic lib
            for g in Group:
                try:
                    TokenManager().delete_session(g.value)
                    cleaned_tokens += 1
                except Exception as e:
                    logger.debug(f"Could not delete session for {g.value}: {e}")

            if cleaned_tokens > 0:
                logger.info(
                    get_string("cleanup_removed").format(
                        f"{cleaned_tokens} Secure Token(s)"
                    )
                )
        except Exception as e:
            logger.warning(get_string("cleanup_secure_fail").format(e))

        # Remove ALL config files (config_*.json)
        # Assuming they are in the current working directory or same dir as the one loaded
        config_dir = self.config_file.parent
        # Find all files matching pattern
        config_files = list(config_dir.glob("config_*.json"))

        if config_files:
            removed_count = 0
            for cf in config_files:
                try:
                    cf.unlink()
                    removed_count += 1
                except Exception as e:
                    logger.error(get_string("cleanup_fail").format(cf.name, e))

            if removed_count > 0:
                logger.info(
                    get_string("cleanup_removed").format(
                        f"{removed_count} Config File(s)"
                    )
                )
        else:
            # Just in case none matched glob but self.config_file exists (edge case)
            if self.config_file.exists():
                try:
                    self.config_file.unlink()
                    logger.info(
                        get_string("cleanup_removed").format(self.config_file.name)
                    )
                except Exception as e:
                    logger.error(
                        get_string("cleanup_fail").format(self.config_file.name, e)
                    )

        # Remove global CLI preferences file
        prefs_file = get_cli_prefs_file()
        if prefs_file.exists():
            try:
                prefs_file.unlink()
                logger.info(get_string("cleanup_removed").format(prefs_file.name))
            except Exception as e:
                logger.error(get_string("cleanup_fail").format(prefs_file.name, e))

        logger.info(get_string("cleanup_done"))
        logger.info(get_string("cleanup_safe_note").format(self.output_dir))

        # Linux-only: Offer to remove Chrome
        if is_linux:
            logger.info(get_string("cleanup_linux_chrome_cmd"))

    async def setup_wizard(self):
        import platform

        is_linux = platform.system() == "Linux"

        # Format title with capitalized group name (e.g., "Hinatazaka46")
        title_str = get_string("setup_title").format(self.group.value.capitalize())
        print(title_str)  # UI Header
        logger.info(get_string("setup_browser"))

        # Use platform-specific user data directory for browser session
        auth_dir = get_auth_dir()
        logger.info(get_string("setup_auth_dir").format(auth_dir))

        # Use Chrome (requires Chrome to be installed)
        try:
            creds = await BrowserAuth.login(
                self.group,
                headless=False,
                user_data_dir=str(auth_dir),
                channel="chrome",
            )
        except Exception as e:
            if "Executable doesn't exist" in str(e) or "chrome" in str(e).lower():
                # Linux: Auto-install Chrome
                if is_linux:
                    logger.warning(get_string("setup_chrome_not_found"))
                    logger.info(get_string("setup_install_chrome"))
                    try:
                        import subprocess
                        import sys

                        result = subprocess.run(
                            [sys.executable, "-m", "playwright", "install", "chrome"],
                            capture_output=False,
                        )
                        if result.returncode != 0:
                            raise Exception(
                                f"playwright install failed with code {result.returncode}"
                            )
                        logger.info(get_string("setup_chrome_success"))
                        creds = await BrowserAuth.login(
                            self.group,
                            headless=False,
                            user_data_dir=str(auth_dir),
                            channel="chrome",
                        )
                    except Exception as install_error:
                        logger.error(
                            get_string("setup_chrome_fail").format(install_error)
                        )
                        logger.error(get_string("setup_manual_linux"))
                        return
                else:
                    # Windows: Prompt user to install manually
                    logger.warning(get_string("setup_chrome_not_found"))
                    logger.error(get_string("setup_manual_other"))
                    return
            else:
                raise e

        if creds:
            # SAVE TO KEYRING / TOKEN MANAGER
            try:
                from pysaka.credentials import TokenManager

                tm = TokenManager()
                tm.save_session(
                    self.group.value,
                    creds["access_token"],
                    creds.get("refresh_token"),
                    creds.get("cookies"),
                )
            except Exception as e:
                logger.error(get_string("error_secure_storage").format(e))
                logger.error(get_string("error_keyring_required"))
                return

            # SAVE NON-SENSITIVE CONFIG (per-group settings)
            config = self.load_config()
            config["auth_dir"] = str(auth_dir)
            config["x-talk-app-id"] = creds.get("x-talk-app-id")
            config["user-agent"] = creds.get("user-agent")
            config["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.save_config(config)

            logger.info(get_string("setup_login_success"))
        else:
            logger.error(get_string("setup_login_fail"))

    async def fetch_blog_members(self) -> dict[str, str]:
        """Fetch available blog members from the official site."""
        import aiohttp
        from pysaka.blog import get_scraper

        async with aiohttp.ClientSession() as session:
            scraper = get_scraper(self.group, session)
            return await scraper.get_members()

    async def fetch_msg_members(self, include_offline: bool = False) -> list[dict]:
        """
        Fetch subscribed message members from the API.
        Requires valid authentication.

        Returns:
            List of group dicts with 'id' and 'name' (member info).
        """
        import aiohttp
        from pysaka.credentials import TokenManager

        config = self.load_config()
        if not config.get("tos_agreed"):
            return []

        try:
            token_data = TokenManager().load_session(self.group.value)
            if not token_data:
                return []

            self.client = Client(
                self.group,
                access_token=token_data.get("access_token"),
                refresh_token=token_data.get("refresh_token"),
                cookies=token_data.get("cookies"),
            )

            async with aiohttp.ClientSession() as session:
                if self.client is None:
                    logger.error("Client not initialized. Please run setup first.")
                    return []
                groups = await self.client.get_groups(
                    session, include_inactive=include_offline
                )
                # Extract member info: id, name, and subscription state
                members = []
                for g in groups:
                    members.append(
                        {
                            "id": g.get("id"),
                            "name": g.get("name", f"Member_{g.get('id')}"),
                            "state": g.get("subscription", {}).get("state"),
                        }
                    )
                return members
        except Exception as e:
            if logger:
                logger.warning(f"Failed to fetch members: {e}")
            return []

    async def run_blog_backup(self, member_ids=None, members_cache=None):
        """Back up official blogs into the same tree SakaDesk's blog sync writes.

        Three stages, mirroring the GUI:
          1. member_thumbnails/ + members_cache.json
          2. blogs/index.json   (list pages only — fast)
          3. blog.json + images/ per post (detail pages)

        Args:
            member_ids: Restrict to these member IDs. None means every member.
            members_cache: Pre-fetched member dict from the interactive wizard,
                kept only to size the scan progress bar.
        """
        import aiohttp

        if not blog_backup.is_blog_supported(self.group):
            logger.error(
                get_string("blog_unsupported_group").format(
                    paths.get_display_name(self.group)
                )
            )
            return

        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Stage 0: member portraits (content-hashed, usually a no-op).
            logger.info(get_string("blog_fetching_members"))
            await blog_backup.sync_member_thumbnails(
                session, self.output_dir, self.group
            )

            # Stage 1: index metadata.
            logger.info(get_string("blog_scanning"))
            target_count = len(member_ids) if member_ids else len(members_cache or {})
            scan_pbar = tqdm(
                total=target_count or None,
                desc=get_string("blog_tqdm_scanning"),
                unit="member",
            )

            def on_member_done(name, new_count):
                scan_pbar.update(1)
                scan_pbar.set_postfix_str(f"{name} (+{new_count})")

            try:
                index = await blog_backup.sync_blog_metadata(
                    session,
                    self.output_dir,
                    self.group,
                    member_ids=member_ids,
                    on_member_done=on_member_done,
                )
            finally:
                scan_pbar.close()

            # Stage 2: content + images.
            queue = blog_backup.build_download_queue(
                self.output_dir, self.group, index, member_ids=member_ids
            )
            if not queue:
                logger.info(get_string("blog_all_cached"))
                return

            logger.info(get_string("blog_downloading"))
            logger.info(get_string("blog_found_posts").format(len(queue)))

            dl_pbar = tqdm(
                total=len(queue), desc=get_string("blog_tqdm_downloading"), unit="post"
            )
            try:
                stats = await blog_backup.download_blog_content(
                    session,
                    self.output_dir,
                    self.group,
                    queue,
                    index=index,
                    write_html=self.write_html,
                    on_item_done=lambda: dl_pbar.update(1),
                )
            finally:
                dl_pbar.close()

            logger.info(
                get_string("blog_stats").format(
                    stats["downloaded"], stats["removed"], stats["failed"]
                )
            )
            logger.info(
                get_string("run_output").format(
                    paths.blogs_root(self.output_dir, self.group).absolute()
                )
            )

    async def run(
        self,
        group_ids=None,
        member_ids=None,
        include_offline=False,
        mode="message",
        blog_members_cache=None,
    ):
        if mode == "blog":
            await self.run_blog_backup(member_ids, members_cache=blog_members_cache)
            return

        from pysaka.credentials import TokenManager

        # 1. LOAD CONFIG & TOKENS
        config = self.load_config()

        try:
            tm = TokenManager()
            token_data = tm.load_session(self.group.value)
        except Exception as e:
            logger.error(get_string("error_secure_storage").format(e))
            logger.error(get_string("error_keyring_required"))
            return

        if not token_data or not token_data.get("access_token"):
            logger.info(get_string("run_no_config"))
            await self.setup_wizard()
            # Reload
            config = self.load_config()
            token_data = tm.load_session(self.group.value)
            if not token_data or not token_data.get("access_token"):
                logger.error(get_string("run_setup_fail"))
                return

        logger.info(get_string("run_init"))
        try:
            import aiohttp

            # Increased limit for parallel fetching
            connector = aiohttp.TCPConnector(limit=30)
            async with aiohttp.ClientSession(connector=connector) as session:
                self.client = Client(
                    group=self.group,
                    access_token=token_data.get("access_token"),
                    refresh_token=token_data.get("refresh_token"),
                    cookies=token_data.get("cookies"),
                    app_id=config.get("x-talk-app-id"),
                    user_agent=config.get("user-agent"),
                    auth_dir=config.get("auth_dir"),
                )

                # Check auth (may raise SessionExpiredError)
                try:
                    auth_ok = await self.client.refresh_access_token(session)
                except SessionExpiredError:
                    # Session invalidated - show clean message and prompt re-login
                    self.show_session_expired()
                    await self.setup_wizard()
                    # Reload config and re-init client
                    config = self.load_config()
                    token_data = tm.load_session(self.group.value)

                    if not token_data or not token_data.get("access_token"):
                        logger.error(get_string("run_setup_fail"))
                        return

                    self.client = Client(
                        group=self.group,
                        access_token=token_data.get("access_token"),
                        refresh_token=token_data.get("refresh_token"),
                        cookies=token_data.get("cookies"),
                        app_id=config.get("x-talk-app-id"),
                        user_agent=config.get("user-agent"),
                        auth_dir=config.get("auth_dir"),
                    )
                    auth_ok = True  # Fresh login, should be valid

                if not auth_ok:
                    if not await self.client.fetch_json(
                        session, "/groups", {"organization_id": 1}
                    ):
                        logger.warning(get_string("run_auth_expired"))
                        await self.setup_wizard()
                        # Reload config and re-init client
                        config = self.load_config()
                        token_data = tm.load_session(self.group.value)

                        if not token_data or not token_data.get("access_token"):
                            logger.error(get_string("run_setup_fail"))
                            return

                        self.client = Client(
                            group=self.group,
                            access_token=token_data.get("access_token"),
                            refresh_token=token_data.get("refresh_token"),
                            cookies=token_data.get("cookies"),
                            app_id=config.get("x-talk-app-id"),
                            user_agent=config.get("user-agent"),
                            auth_dir=config.get("auth_dir"),
                        )

                        # Just verify connectivity, don't force refresh immediately
                        if not await self.client.fetch_json(
                            session, "/groups", {"organization_id": 1}
                        ):
                            logger.error(get_string("run_auth_fail_post_setup"))
                            return
                        else:
                            logger.info(get_string("run_auth_verified"))

                # Check if token changed during refresh and save it
                if self.client.access_token != token_data.get("access_token"):
                    tm.save_session(
                        self.group.value,
                        self.client.access_token,
                        self.client.refresh_token,
                        self.client.cookies,
                    )
                    logger.debug("Tokens refreshed and saved to secure storage.")

                # SyncManager writes messages/ and sync_state.json relative to
                # the root it is given, so hand it the per-service root rather
                # than the bare output dir — that is what puts the tree at
                # {output}/日向坂46/messages/... like SakaDesk's.
                self.service_root.mkdir(parents=True, exist_ok=True)
                self.manager = SyncManager(self.client, self.service_root)

                metadata_file = paths.sync_metadata_path(self.output_dir, self.group)
                metadata = await meta_store.load_metadata(metadata_file)

                # Phase 1: Discovery
                logger.info(get_string("run_phase_1").strip())
                scan_pbar = tqdm(desc=get_string("run_tqdm_scanning"), unit="group")
                try:
                    tasks = await message_sync.discover_tasks(
                        session,
                        self.client,
                        metadata,
                        group_ids=group_ids,
                        member_ids=member_ids,
                        include_offline=include_offline,
                        on_group_done=lambda: scan_pbar.update(1),
                    )
                finally:
                    scan_pbar.close()

                if not tasks:
                    logger.warning(get_string("run_no_groups"))
                    return

                logger.info(
                    get_string("run_found_timelines").format(len(tasks)).strip()
                )

                # Phase 2: one timeline fetch per group, distributed to members
                logger.info(get_string("run_phase_2"))
                media_queue = []
                pbar = tqdm(
                    total=len(tasks),
                    desc=get_string("run_tqdm_fetching"),
                    unit="member",
                )

                def on_member_done(name, count):
                    pbar.update(1)
                    pbar.set_postfix_str(f"{name} ({count})")

                try:
                    total_new, members_with_new = await message_sync.sync_messages(
                        session,
                        self.manager,
                        tasks,
                        media_queue,
                        metadata,
                        initial_limit=self.initial_limit,
                        on_member_done=on_member_done,
                    )
                finally:
                    pbar.close()

                # Phase 3: Media
                if media_queue:
                    logger.info(
                        get_string("run_phase_3").format(len(media_queue)).strip()
                    )
                    with tqdm(total=len(media_queue), unit="file") as media_pbar:
                        await message_sync.download_media(
                            session,
                            self.manager,
                            media_queue,
                            on_chunk_done=media_pbar.update,
                        )

                # Written last so last_sync only advances on a completed run.
                meta_store.stamp_last_sync(metadata)
                await meta_store.save_metadata(metadata_file, metadata)

                logger.info(get_string("run_done").strip())
                logger.info(
                    get_string("run_new_messages").format(total_new, members_with_new)
                )
                logger.info(
                    get_string("run_output").format(self.service_root.absolute())
                )


        except Exception as e:
            logger.error(get_string("run_error").format(e))
            logger.debug(traceback.format_exc())


def get_parser():
    parser = argparse.ArgumentParser(
        prog="saka-cli", description=get_string("help_desc")
    )
    # Updated order: nogizaka, sakurazaka, hinatazaka
    parser.add_argument(
        "-s",
        "--service",
        type=str,
        choices=["nogizaka46", "sakurazaka46", "hinatazaka46"],
        help=get_string("help_service"),
    )
    parser.add_argument("-o", "--output", type=str, help=get_string("help_output"))
    parser.add_argument(
        "-i", "--interactive", action="store_true", help=get_string("help_interactive")
    )
    parser.add_argument(
        "--include-offline", action="store_true", help=get_string("help_offline")
    )
    parser.add_argument("-g", "--group", type=str, help=get_string("help_group"))
    parser.add_argument(
        "-m", "--members", type=str, nargs="*", help=get_string("help_members")
    )
    parser.add_argument(
        "--cleanup", action="store_true", help=get_string("help_cleanup")
    )
    parser.add_argument(
        "--lang",
        type=str,
        choices=["en", "ja", "zh-TW", "zh-CN", "yue"],
        help=get_string("help_lang"),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help=get_string("help_verbose")
    )
    parser.add_argument("--blog", action="store_true", help=get_string("help_blog"))
    parser.add_argument("--html", action="store_true", help=get_string("help_html"))
    parser.add_argument(
        "--initial-limit",
        type=int,
        default=message_sync.DEFAULT_INITIAL_MESSAGE_LIMIT,
        help=get_string("help_initial_limit"),
    )
    parser.add_argument("--ask", type=str, metavar="QUERY", help=get_string("help_ask"))
    parser.add_argument(
        "--kb-index", action="store_true", help=get_string("help_kb_index")
    )
    parser.add_argument(
        "--kb-format",
        type=str,
        choices=["text", "json", "brief"],
        default="text",
        help=get_string("help_kb_format"),
    )
    parser.add_argument(
        "--kb-members", type=str, metavar="DIR", help=get_string("help_kb_members")
    )
    parser.add_argument(
        "--kb-aliases", type=str, metavar="FILE", help=get_string("help_kb_aliases")
    )
    parser.add_argument(
        "--kb-call-names",
        type=str,
        metavar="PATH",
        help=get_string("help_kb_call_names"),
    )
    parser.add_argument(
        "--llm-backend",
        type=str,
        choices=["cloud", "local"],
        help=get_string("help_llm_backend"),
    )
    parser.add_argument(
        "--llm-base-url", type=str, metavar="URL", help=get_string("help_llm_base_url")
    )
    parser.add_argument(
        "--llm-model", type=str, metavar="NAME", help=get_string("help_llm_model")
    )
    parser.add_argument(
        "--hw-suggest", action="store_true", help=get_string("help_hw_suggest")
    )
    return parser


def peek_language_from_argv() -> Optional[str]:
    """Manual peek at argv to set language before argparse runs."""
    try:
        if "--lang" in sys.argv:
            idx = sys.argv.index("--lang")
            if idx + 1 < len(sys.argv):
                return sys.argv[idx + 1]
    except Exception:
        pass
    return None


def main():
    # 0. EARLY LANGUAGE DETECTION

    # A. Flag Peek
    lang = peek_language_from_argv()

    # B. Global CLI Preferences
    if not lang:
        try:
            prefs = load_cli_prefs()
            lang = prefs.get("lang")
        except Exception:
            pass

    # C. System Detect
    if not lang:
        lang = detect_system_language()

    # Apply Language
    set_language(lang)
    # logger is None here, so we skip debug log or use print if strictly needed,
    # but detection details are minor.

    # 1. Parse Args (Now with localized help)
    parser = get_parser()

    # Auto-enable interactive mode if no arguments provided (Double-Click behavior)
    if len(sys.argv) == 1:
        sys.argv.append("--interactive")

    args = parser.parse_args()

    # Re-apply strict args.lang if it was parsed
    if args.lang:
        set_language(args.lang)

    # Setup Logging
    setup_logging(verbose=args.verbose)

    # Refresh logger after setup to ensure it picks up the new configuration (including timestamp format)
    global logger
    logger = structlog.get_logger()

    logger.debug(f"Language pre-initialized to: {lang}")

    # --hw-suggest: diagnostic-only. Detect hardware, print it + the local-vs-cloud LLM
    # backend suggestion, and exit -- doesn't require --service or touch any other flag.
    if args.hw_suggest:
        hw = detect_hardware()
        suggestion = suggest_llm_backend(hw)
        print(
            json.dumps(
                {"hardware": hw, "suggestion": suggestion}, ensure_ascii=False, indent=2
            )
        )
        return

    # Initialize Defaults
    service_str = args.service
    output_dir = args.output
    include_offline = args.include_offline
    mode = "blog" if args.blog else "message"

    # Knowledge-base harness (--ask / --kb-index): bypasses the sync/wizard path
    # entirely, since it only reads already-synced data.
    if args.service and (args.ask or args.kb_index):
        if not args.kb_members:
            logger.error(get_string("kb_error_members_required"))
            return
        if not args.llm_backend:
            # No explicit --llm-backend: log the hardware-based suggestion as a hint so the
            # user knows what they *could* pass, without forcing a choice on them.
            hint = suggest_llm_backend()
            logger.info(
                get_string("kb_llm_hint").format(hint["recommended"], hint["reason"])
            )
        try:
            run_kb_command(
                output_dir=Path(output_dir or DEFAULT_OUTPUT),
                service=args.service,
                members_dir=Path(args.kb_members),
                ask=args.ask,
                kb_index=args.kb_index,
                kb_format=args.kb_format,
                aliases_file=Path(args.kb_aliases) if args.kb_aliases else None,
                call_names_file=Path(args.kb_call_names)
                if args.kb_call_names
                else None,
                llm_backend=args.llm_backend,
                llm_base_url=args.llm_base_url,
                llm_model=args.llm_model,
            )
        except KeyboardInterrupt:
            logger.warning(get_string("run_interrupted"))
        except Exception as e:
            logger.critical(get_string("run_fatal").format(e))
            logger.debug(traceback.format_exc())
        return

    # Parse group IDs (comma or space-separated)
    group_ids = []
    if args.group:
        group_ids = parse_int_list(args.group)

    # Parse member IDs (comma or space-separated, can be multiple args)
    member_ids = []
    if args.members:
        for m in args.members:
            member_ids.extend(parse_id_list(m))

    # Interactive Mode overrides
    # Enable if explicitly requested OR implicitly if no service specified (and not cleanup)
    if args.interactive or (not args.service and not args.cleanup):
        cli_dummy = SakaCLI()  # Temp instance to run wizard
        wizard_config = cli_dummy.run_interactive_wizard(force_lang=bool(args.lang))

        service_str = wizard_config.get("service", service_str)
        output_dir = wizard_config.get("output_dir", output_dir)
        include_offline = wizard_config.get("include_offline", include_offline)
        mode = wizard_config.get("mode", mode)

        # Interactive wizard returns single group_id, convert to list
        wizard_group = wizard_config.get("group_id")
        if wizard_group:
            group_ids = [wizard_group]

        # Members from wizard
        # For 'blog', these are Member IDs.
        # For 'message', these are Group/Subscription IDs.
        w_members = wizard_config.get("members")
        if w_members:
            if mode == "blog":
                member_ids = w_members
            else:
                group_ids = w_members

        # Blog members cache from wizard (avoids duplicate fetch)
        blog_members_cache = wizard_config.get("_blog_members_cache")
    else:
        blog_members_cache = None

    # Defaults
    if not output_dir:
        output_dir = DEFAULT_OUTPUT
    if not service_str:
        service_str = "nogizaka46"  # Default is now Nogizaka46

    target_group = Group(service_str)

    cli = SakaCLI(
        output_dir=output_dir,
        group=target_group,
        write_html=args.html,
        initial_limit=args.initial_limit,
    )

    if args.cleanup:
        asyncio.run(cli.cleanup_wizard())
        return

    # Check ToS before proceeding to ANY operation that accesses the service
    if not cli.check_tos():
        return

    try:
        asyncio.run(
            cli.run(
                group_ids=group_ids if group_ids else None,
                member_ids=member_ids if member_ids else None,
                include_offline=include_offline,
                mode=mode,
                blog_members_cache=blog_members_cache,
            )
        )
    except KeyboardInterrupt:
        logger.warning(get_string("run_interrupted"))
    except Exception as e:
        logger.critical(get_string("run_fatal").format(e))
        logger.debug(traceback.format_exc())

    if args.interactive and sys.platform == "win32":
        input(get_string("interactive_exit"))


if __name__ == "__main__":
    sys.exit(main())
