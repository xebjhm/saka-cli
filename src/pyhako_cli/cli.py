
import asyncio
import argparse
import json
import traceback
import sys
import structlog
from pathlib import Path
from tqdm import tqdm
from datetime import datetime, timezone
import locale
import ctypes
from pyhako import Client, BrowserAuth, SyncManager, Group, get_auth_dir

from pyhako_cli.logging_setup import setup_logging
from pyhako_cli.strings import get_string, set_language, get_language

# Logger initialized in main()
logger = None # Placeholder

def detect_system_language():
    """Detect system language in a cross-platform way."""
    lang_code = "en"
    try:
        # Priority 1: Environment Variables (Standard override)
        import os
        lang_sys = os.environ.get("LANG") or os.environ.get("LANGUAGE") or os.environ.get("LC_ALL")
        
        # Priority 2: locale module (OS Default) - use getlocale() instead of deprecated getdefaultlocale()
        if not lang_sys:
            try:
                locale.setlocale(locale.LC_ALL, '')  # Initialize from environment
                lang_sys = locale.getlocale()[0]
            except:
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

def parse_int_list(value: str) -> list[int]:
    """Parse comma or space-separated integers."""
    if ',' in value:
        return [int(x.strip()) for x in value.split(',') if x.strip()]
    return [int(value)]

class HakoCLI:
    def __init__(self, output_dir: str = DEFAULT_OUTPUT, group: Group = Group.HINATAZAKA46):
        self.output_dir = Path(output_dir)
        self.group = group
        self.config_file = Path(f"config_{group.value}.json")
        self.client = None
        self.manager = None 

    def load_config(self) -> dict:
        if self.config_file.exists():
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def save_config(self, config: dict):
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)

    def check_tos(self) -> bool:
        """
        Check if user agreed to ToS. If not, prompt and block.
        Returns True if agreed, False if declined.
        """
        config = self.load_config()
        if config.get('tos_agreed'):
            return True
            
        # Get link based on group value
        group_val = self.group.value
        if group_val == 'sakurazaka46':
            link = "https://sakurazaka46.com/s/s46app/page/app_terms"
        elif group_val == 'hinatazaka46':
            link = "https://www.hinatazaka46.com/s/h46app/page/app_terms"
        else: # nogizaka46 (default)
            link = "https://contact.nogizaka46.com/s/n46app/page/app_terms"

        # Display ToS
        print(get_string("tos_title"))
        print(get_string("tos_warn").format(link))
        
        # Block
        try:
             choice = input(get_string("tos_prompt")).strip().lower()
        except EOFError:
             # Handle non-interactive environments that didn't pre-agree
             choice = 'n'

        if choice == 'y':
            config['tos_agreed'] = True
            self.save_config(config)
            return True
        else:
            print(get_string("tos_declined"))
            return False

    def run_interactive_wizard(self):
        """
        Interactive wizard to gather configuration from the user.
        Returns a dictionary with configuration keys.
        """
        # USE PRINT FOR UI ELEMENTS
        print(get_string("interactive_title"))
        
        config = {}

        # -1. Language Selection (Skip if already auto-detected)
        current_lang = get_language()
        if current_lang != 'en':
            # Language was already detected from config/system - use it silently
            selected_lang = current_lang
        else:
            # First-time or English default - show prompt
            print(get_string("interactive_lang"), end="")
            lang_choice = input().strip()
            selected_lang = 'en'  # Default
            if lang_choice:
                if lang_choice == '2': selected_lang = 'ja'
                elif lang_choice == '3': selected_lang = 'zh-TW'
                elif lang_choice == '4': selected_lang = 'zh-CN'
                elif lang_choice == '5': selected_lang = 'yue'
            set_language(selected_lang)
        config['lang'] = selected_lang  # Store for saving

        # 0. Service Selection
        print(get_string("interactive_service"), end="")
        service_choice = input().strip()
        if service_choice == '2':
            config['service'] = 'sakurazaka46'
        elif service_choice == '3':
            config['service'] = 'hinatazaka46'
        else:
            config['service'] = 'nogizaka46'
        
        # [Fix] Check ToS immediately after service selection (before proceeding)
        # We need to check the config for the SELECTED service, not the default one.
        temp_group = Group(config['service'])
        temp_cli = HakoCLI(group=temp_group)
        if not temp_cli.check_tos():
             sys.exit(0) # Exit if declined in wizard

        # 1. Output Directory
        user_out = input(get_string("interactive_out").format(DEFAULT_OUTPUT)).strip()
        config['output_dir'] = user_out if user_out else DEFAULT_OUTPUT
            
        # 2. Offline Members
        user_off = input(get_string("interactive_offline")).strip().lower()
        config['include_offline'] = (user_off == 'y')

        # 3. Group ID
        user_group = input(get_string("interactive_group_id")).strip()
        if user_group:
            try:
                config['group_id'] = int(user_group)
            except ValueError:
                 logger.warning(get_string("invalid_group_id"))
                 config['group_id'] = None
        else:
            config['group_id'] = None

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
        if confirm.lower() != 'y':
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
            from pyhako.credentials import TokenManager
            from pyhako.client import Group
            
            cleaned_tokens = 0
            # Iterate over all defined groups in the generic lib
            for g in Group:
                try:
                    TokenManager().delete_session(g.value)
                    cleaned_tokens += 1
                except:
                    pass
            
            if cleaned_tokens > 0:
                logger.info(get_string("cleanup_removed").format(f"{cleaned_tokens} Secure Token(s)"))
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
                logger.info(get_string("cleanup_removed").format(f"{removed_count} Config File(s)"))
        else:
             # Just in case none matched glob but self.config_file exists (edge case)
            if self.config_file.exists():
                try:
                    self.config_file.unlink()
                    logger.info(get_string("cleanup_removed").format(self.config_file.name))
                except Exception as e:
                    logger.error(get_string("cleanup_fail").format(self.config_file.name, e))


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
        print(title_str) # UI Header
        logger.info(get_string("setup_browser"))
        
        # Use platform-specific user data directory for browser session
        auth_dir = get_auth_dir()
        logger.info(get_string("setup_auth_dir").format(auth_dir))
        
        # Use Chrome (requires Chrome to be installed)
        try:
            creds = await BrowserAuth.login(self.group, headless=False, user_data_dir=str(auth_dir), channel="chrome")
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
                            capture_output=False
                        )
                        if result.returncode != 0:
                            raise Exception(f"playwright install failed with code {result.returncode}")
                        logger.info(get_string("setup_chrome_success"))
                        creds = await BrowserAuth.login(self.group, headless=False, user_data_dir=str(auth_dir), channel="chrome")
                    except Exception as install_error:
                        logger.error(get_string("setup_chrome_fail").format(install_error))
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
                from pyhako.credentials import TokenManager
                tm = TokenManager() 
                tm.save_session(
                    self.group.value, 
                    creds['access_token'],
                    creds.get('refresh_token'),
                    creds.get('cookies')
                )
            except Exception as e:
                logger.error(get_string("error_secure_storage").format(e))
                logger.error(get_string("error_keyring_required"))
                return

            # SAVE NON-SENSITIVE CONFIG
            config = self.load_config()
            config['auth_dir'] = str(auth_dir)
            config['x-talk-app-id'] = creds.get('x-talk-app-id')
            config['user-agent'] = creds.get('user-agent')
            config['updated_at'] = datetime.now(timezone.utc).isoformat()
            config['lang'] = get_language()
            self.save_config(config)
            
            logger.info(get_string("setup_login_success"))
        else:
            logger.error(get_string("setup_login_fail"))

    async def process_member(self, session, task, media_queue, pbar):
        # Wrapper to call manager.sync_member
        async def on_progress(date_str, count):
            pbar.set_postfix_str(f"Latest: {task['member']['name']} ({count})")
            
        await self.manager.sync_member(
            session, 
            task['group'], 
            task['member'], 
            media_queue, 
            progress_callback=on_progress
        )

    async def run(self, group_ids=None, member_ids=None, include_inactive=False):
        from pyhako.credentials import TokenManager
        
        # 1. LOAD CONFIG & TOKENS
        config = self.load_config()
        
        try:
            tm = TokenManager()
            token_data = tm.load_session(self.group.value)
        except Exception as e:
             logger.error(get_string("error_secure_storage").format(e))
             logger.error(get_string("error_keyring_required"))
             return

        if not token_data or not token_data.get('access_token'):
            logger.info(get_string("run_no_config"))
            await self.setup_wizard()
            # Reload
            config = self.load_config()
            token_data = tm.load_session(self.group.value)
            if not token_data or not token_data.get('access_token'):
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
                    access_token=token_data.get('access_token'),
                    refresh_token=token_data.get('refresh_token'),
                    cookies=token_data.get('cookies'),
                    app_id=config.get('x-talk-app-id'),
                    user_agent=config.get('user-agent'),
                    auth_dir=config.get('auth_dir')
                )
                
                # Check auth
                if not await self.client.refresh_access_token(session):
                     if not await self.client.fetch_json(session, "/groups", {"organization_id": 1}):
                          logger.warning(get_string("run_auth_expired"))
                          await self.setup_wizard()
                          # Reload config and re-init client
                          config = self.load_config()
                          token_data = tm.load_session(self.group.value)
                          
                          if not token_data or not token_data.get('access_token'):
                              logger.error(get_string("run_setup_fail"))
                              return
                              
                          self.client = Client(
                             group=self.group,
                             access_token=token_data.get('access_token'),
                             refresh_token=token_data.get('refresh_token'),
                             cookies=token_data.get('cookies'),
                             app_id=config.get('x-talk-app-id'),
                             user_agent=config.get('user-agent'),
                             auth_dir=config.get('auth_dir')
                          )
                          
                          # Just verify connectivity, don't force refresh immediately
                          if not await self.client.fetch_json(session, "/groups", {"organization_id": 1}):
                              logger.error(get_string("run_auth_fail_post_setup"))
                              return
                          else:
                              logger.info(get_string("run_auth_verified"))

                # Check if token changed during refresh and save it
                if self.client.access_token != token_data.get('access_token'):
                    tm.save_session(
                        self.group.value,
                        self.client.access_token,
                        self.client.refresh_token,
                        self.client.cookies
                    )
                    logger.debug("Tokens refreshed and saved to secure storage.")

                # Initialize SyncManager
                self.manager = SyncManager(self.client, self.output_dir)

                # 2. Discovery
                target_groups = []
                # Always fetch groups to get proper names for folder creation
                all_groups = await self.client.get_groups(session, include_inactive=True)
                if group_ids:
                    # Find specified groups from fetched list to get proper names
                    target_groups = [g for g in all_groups if g['id'] in group_ids]
                    # Fallback for groups not in subscription list
                    found_ids = {g['id'] for g in target_groups}
                    for gid in group_ids:
                        if gid not in found_ids:
                            target_groups.append({'id': gid, 'name': str(gid)})
                else:
                    # Use normal filtering for "scan all" mode
                    target_groups = await self.client.get_groups(session, include_inactive)

                if not target_groups:
                    logger.warning(get_string("run_no_groups"))
                    return

                logger.info(get_string("run_phase_1").strip())
                tasks = []
                for group in tqdm(target_groups, desc="Scanning Groups"):
                    members = await self.client.get_members(session, group['id'])
                    if not members:
                        continue
                    if member_ids:
                        members = [m for m in members if m['id'] in member_ids]
                    for m in members:
                        tasks.append({'group': group, 'member': m})
                
                logger.info(get_string("run_found_timelines").format(len(tasks)).strip())
                
                # Setup Output
                self.output_dir.mkdir(parents=True, exist_ok=True)

                # Phase 2: Metadata (Parallel)
                logger.info(get_string("run_phase_2"))
                media_queue = []
                
                sem = asyncio.Semaphore(5) # Concurrency Limit
                
                async def worker(t, pbar):
                    async with sem:
                        await self.process_member(session, t, media_queue, pbar)
                        pbar.update(1)

                pbar = tqdm(total=len(tasks), desc="Fetching Members", unit="member")
                await asyncio.gather(*[worker(t, pbar) for t in tasks])
                pbar.close()

                # Phase 3: Media
                if media_queue:
                    logger.info(get_string("run_phase_3").format(len(media_queue)).strip())
                    chunk_size = 50
                    with tqdm(total=len(media_queue), unit="file") as pbar:
                        for i in range(0, len(media_queue), chunk_size):
                            chunk = media_queue[i:i+chunk_size]
                            
                            await self.manager.process_media_queue(session, chunk, concurrency=5)
                            pbar.update(len(chunk))
                
                logger.info(get_string("run_done").strip())
                logger.info(get_string("run_output").format(self.output_dir.absolute()))

        except Exception as e:
            logger.error(get_string("run_error").format(e))
            logger.debug(traceback.format_exc())

def get_parser():
    parser = argparse.ArgumentParser(description=get_string("help_desc"))
    # Updated order: nogizaka, sakurazaka, hinatazaka
    parser.add_argument('-s', '--service', type=str, choices=['nogizaka46', 'sakurazaka46', 'hinatazaka46'], help=get_string("help_service"))
    parser.add_argument('-o', '--output', type=str, help=get_string("help_output"))
    parser.add_argument('-i', '--interactive', action='store_true', help=get_string("help_interactive"))
    parser.add_argument('--include-offline', action='store_true', help=get_string("help_offline"))
    parser.add_argument('-g', '--group', type=str, help=get_string("help_group"))
    parser.add_argument('-m', '--members', type=str, nargs='*', help=get_string("help_members"))
    parser.add_argument('--cleanup', action='store_true', help=get_string("help_cleanup"))
    parser.add_argument('--lang', type=str, choices=['en', 'ja', 'zh-TW', 'zh-CN', 'yue'], help=get_string("help_lang"))
    parser.add_argument('-v', '--verbose', action='store_true', help=get_string("help_verbose"))
    return parser

def peek_language_from_argv() -> str:
    """Manual peek at argv to set language before argparse runs."""
    try:
        if '--lang' in sys.argv:
            idx = sys.argv.index('--lang')
            if idx + 1 < len(sys.argv):
                return sys.argv[idx + 1]
    except:
        pass
    return None

def main():
    # 0. EARLY LANGUAGE DETECTION
    
    # A. Flag Peek
    lang = peek_language_from_argv()
    
    # B. Config Peek (Check ALL config files for lang)
    if not lang:
        try:
            for config_path in Path('.').glob('config_*.json'):
                try:
                    with open(config_path, encoding='utf-8') as f:
                        cfg = json.load(f)
                        if cfg.get('lang'):
                            lang = cfg['lang']
                            break
                except:
                    pass
        except:
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
        sys.argv.append('--interactive')
        
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
    
    # Initialize Defaults
    service_str = args.service
    output_dir = args.output
    include_offline = args.include_offline
    
    # Parse group IDs (comma or space-separated)
    group_ids = []
    if args.group:
        group_ids = parse_int_list(args.group)
    
    # Parse member IDs (comma or space-separated, can be multiple args)
    member_ids = []
    if args.members:
        for m in args.members:
            member_ids.extend(parse_int_list(m))

    # Interactive Mode overrides
    if args.interactive:
        cli_dummy = HakoCLI() # Temp instance to run wizard
        wizard_config = cli_dummy.run_interactive_wizard()
        
        service_str = wizard_config.get('service', service_str)
        output_dir = wizard_config.get('output_dir', output_dir)
        include_offline = wizard_config.get('include_offline', include_offline)
        # Interactive wizard returns single group_id, convert to list
        wizard_group = wizard_config.get('group_id')
        if wizard_group:
            group_ids = [wizard_group]
    
    # Defaults
    if not output_dir:
        output_dir = DEFAULT_OUTPUT
    if not service_str:
        service_str = 'nogizaka46' # Default is now Nogizaka46
        
    target_group = Group(service_str)

    cli = HakoCLI(output_dir=output_dir, group=target_group)
    
    if args.cleanup:
        asyncio.run(cli.cleanup_wizard())
        return

    # Check ToS before proceeding to ANY operation that accesses the service
    if not cli.check_tos():
        return

    try:
        asyncio.run(cli.run(
            group_ids=group_ids if group_ids else None, 
            member_ids=member_ids if member_ids else None, 
            include_inactive=include_offline 
        ))
    except KeyboardInterrupt:
        logger.warning(get_string("run_interrupted"))
    except Exception as e:
        logger.critical(get_string("run_fatal").format(e))
        logger.debug(traceback.format_exc())

    if args.interactive and sys.platform == "win32":
         input(get_string("interactive_exit"))

if __name__ == "__main__":
    sys.exit(main())
