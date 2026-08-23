import json

import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from saka_cli.cli import main
from saka_cli.message_sync import DEFAULT_INITIAL_MESSAGE_LIMIT


@pytest.fixture(autouse=True)
def mock_dependencies():
    with (
        patch("saka_cli.cli.setup_logging"),
        patch("saka_cli.cli.Client"),
        patch("saka_cli.cli.SyncManager"),
        patch("saka_cli.cli.BrowserAuth"),
    ):
        yield


def test_cli_help():
    """Test that the CLI can run --help without error."""
    with patch("sys.argv", ["saka-cli", "--help"]):
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0


def test_cli_no_args_triggers_interactive():
    """Test that no args results in interactive mode."""
    with patch("sys.argv", ["saka-cli"]), patch("saka_cli.cli.SakaCLI") as MockCLI:
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()
        mock_instance.run_interactive_wizard.return_value = {"service": "hinatazaka46"}

        main()

        # Should have called interactive wizard
        mock_instance.run_interactive_wizard.assert_called_once()


def test_cli_batch_mode():
    """Test explicit batch mode execution."""
    with (
        patch("sys.argv", ["saka-cli", "-o", "outdir", "-s", "nogizaka46"]),
        patch("saka_cli.cli.SakaCLI") as MockCLI,
    ):
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()

        main()

        # Should NOT call wizard
        mock_instance.run_interactive_wizard.assert_not_called()

        from pysaka import Group

        # Should initialize CLI with passed args
        MockCLI.assert_called_with(
            output_dir="outdir",
            group=Group.NOGIZAKA46,
            write_html=False,
            initial_limit=DEFAULT_INITIAL_MESSAGE_LIMIT,
        )
        # Should run
        mock_instance.run.assert_called_once()


def test_cli_cleanup_flag():
    """Test that --cleanup triggers cleanup_wizard."""
    with (
        patch("sys.argv", ["saka-cli", "--cleanup"]),
        patch("saka_cli.cli.SakaCLI") as MockCLI,
    ):
        mock_instance = MockCLI.return_value
        mock_instance.cleanup_wizard = AsyncMock()

        main()

        mock_instance.cleanup_wizard.assert_called_once()


def test_cli_interactive_flag():
    """Test that --interactive triggers the wizard."""
    with (
        patch("sys.argv", ["saka-cli", "--interactive"]),
        patch("saka_cli.cli.SakaCLI") as MockCLI,
    ):
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()
        # Mock the wizard to return specific config
        mock_instance.run_interactive_wizard.return_value = {
            "output_dir": "custom_out",
            "include_offline": True,
            "group_id": 123,
            "service": "nogizaka46",
        }

        main()

        # Should call wizard
        mock_instance.run_interactive_wizard.assert_called_once()

        # Should initialize CLI with the wizard's output (and appropriate Group)
        # Note: We need to check if ScraperCLI was initialized with Group.NOGIZAKA46
        # Since we mock ScraperCLI class, we can check call args
        from pysaka import Group

        MockCLI.assert_called_with(
            output_dir="custom_out",
            group=Group.NOGIZAKA46,
            write_html=False,
            initial_limit=DEFAULT_INITIAL_MESSAGE_LIMIT,
        )

        # Should run with wizard's config (include_offline is the actual param name, mode is also passed)
        mock_instance.run.assert_called_once_with(
            group_ids=[123],
            member_ids=None,
            include_offline=True,
            mode="message",
            blog_members_cache=None,
        )


def test_cli_lang_arg():
    """Test that --lang flag sets the language."""
    from saka_cli import strings

    # Need to also provide -s to skip interactive mode (which would need wizard mock)
    with (
        patch("sys.argv", ["saka-cli", "--lang", "ja", "-s", "nogizaka46"]),
        patch("saka_cli.cli.SakaCLI") as MockCLI,
    ):
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()

        main()

        # Verify language was set.
        # Since string module is imported, we can check its internal state or check if get_string returns ja strings.
        assert strings._CURRENT_LANG == "ja"

        # Verify reset for other tests
        strings.set_language("en")


def test_cli_lang_detection():
    """Test that language detection works (mocking locale)."""
    from saka_cli.cli import detect_system_language

    # Test Japanese detection
    with (
        patch("locale.setlocale"),
        patch("locale.getlocale", return_value=("ja_JP", "UTF-8")),
        patch("os.environ.get", return_value=None),
    ):
        assert detect_system_language() == "ja"

    # Test Traditional Chinese
    with (
        patch("locale.setlocale"),
        patch("locale.getlocale", return_value=("zh_TW", "UTF-8")),
        patch("os.environ.get", return_value=None),
    ):
        assert detect_system_language() == "zh-TW"

    # Test Simplified Chinese (fallback)
    with (
        patch("locale.setlocale"),
        patch("locale.getlocale", return_value=("zh_CN", "UTF-8")),
        patch("os.environ.get", return_value=None),
    ):
        assert detect_system_language() == "zh-CN"


def test_cli_full_batch_mode():
    """Test batch mode with ALL optional arguments (group, members, offline)."""
    # Args: -o out -s sakurazaka46 -g 100 -m 1 2 3 --include-offline
    argv = [
        "saka-cli",
        "-o",
        "custom_out",
        "-s",
        "sakurazaka46",
        "-g",
        "100",
        "-m",
        "1",
        "2",
        "3",
        "--include-offline",
    ]

    with patch("sys.argv", argv), patch("saka_cli.cli.SakaCLI") as MockCLI:
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()

        main()

        from pysaka import Group

        # Check Init
        MockCLI.assert_called_with(
            output_dir="custom_out",
            group=Group.SAKURAZAKA46,
            write_html=False,
            initial_limit=DEFAULT_INITIAL_MESSAGE_LIMIT,
        )

        # Check Run Args (include_offline is the correct param, mode is always passed)
        mock_instance.run.assert_called_once_with(
            group_ids=[100],
            # Group IDs are ints; member IDs stay strings because blog member
            # IDs are site codes that int() would mangle (see cli.parse_id_list).
            member_ids=["1", "2", "3"],
            include_offline=True,
            mode="message",
            blog_members_cache=None,
        )


def test_cli_verbose():
    """Test that -v flag enables verbose logging."""
    with (
        patch("sys.argv", ["saka-cli", "-v"]),
        patch("saka_cli.cli.setup_logging") as mock_setup,
        patch("saka_cli.cli.SakaCLI") as MockCLI,
    ):  # Mock CLI to prevent run
        # We need to mock instance methods to avoid main() crashing after setup_logging
        MockCLI.return_value.run = AsyncMock()
        MockCLI.return_value.run_interactive_wizard.return_value = {}

        main()

        mock_setup.assert_called_with(verbose=True)


def test_interactive_wizard_skips_lang_prompt_when_detected():
    """Test that interactive wizard skips lang prompt when language was pre-detected (non-English)."""
    from saka_cli.cli import SakaCLI
    from saka_cli import strings

    # Set language to Japanese (simulating auto-detection from system/config)
    original_lang = strings.get_language()
    strings.set_language("ja")

    try:
        cli = SakaCLI()

        # Mock input to track what prompts are shown
        inputs_given = []

        def mock_input(prompt=""):
            inputs_given.append(prompt)
            # Return 'y' for ToS prompt, empty for everything else
            if "y/N" in prompt or "[y/N]" in prompt:
                return "y"
            return ""  # Default for all prompts

        with patch("builtins.input", side_effect=mock_input), patch("builtins.print"):
            cli.run_interactive_wizard()

        # Should NOT prompt for language (skipped), so fewer inputs than when English
        # Language is stored in global prefs, not in config anymore
        # Verify language was preserved (not overwritten) by checking strings module
        assert strings.get_language() == "ja"
    finally:
        # Reset language
        strings.set_language(original_lang)


# ---------------------------------------------------------------------------
# --hw-suggest / --llm-backend wiring
# ---------------------------------------------------------------------------


def test_hw_suggest_prints_hardware_and_suggestion_then_exits(capsys):
    """--hw-suggest prints detect_hardware() + suggest_llm_backend() as JSON and returns
    without touching the sync/wizard/kb paths.

    Patches `structlog.get_logger` (not `saka_cli.cli.logger` directly) because `main()`
    rebinds the module-level `logger` via `global logger; logger = structlog.get_logger()`
    right after `setup_logging()` -- patching the name alone would just get overwritten by
    that rebind. This also silences the unrelated "Language pre-initialized" debug line so
    capsys only contains the JSON payload.
    """
    fake_hw = {
        "ram_gb": 32.0,
        "gpu": "NVIDIA RTX 3060",
        "vram_gb": 12.0,
        "platform": "Linux",
    }
    fake_suggestion = {
        "recommended": "local",
        "local_model": "qwen2.5:14b",
        "tier": "T1",
        "reason": "test reason",
    }

    with (
        patch("sys.argv", ["saka-cli", "--hw-suggest"]),
        patch("saka_cli.cli.structlog.get_logger"),
        patch("saka_cli.cli.detect_hardware", return_value=fake_hw) as mock_detect,
        patch(
            "saka_cli.cli.suggest_llm_backend", return_value=fake_suggestion
        ) as mock_suggest,
        patch("saka_cli.cli.SakaCLI") as MockCLI,
    ):
        main()

    mock_detect.assert_called_once()
    mock_suggest.assert_called_once_with(fake_hw)
    MockCLI.assert_not_called()

    payload = json.loads(capsys.readouterr().out)
    assert payload["hardware"] == fake_hw
    assert payload["suggestion"] == fake_suggestion


def test_kb_harness_passes_llm_flags_through_to_run_kb_command():
    """--llm-backend/--llm-base-url/--llm-model reach run_kb_command unchanged."""
    with (
        patch(
            "sys.argv",
            [
                "saka-cli",
                "--service",
                "hinatazaka46",
                "--kb-index",
                "--kb-members",
                "members_dir",
                "--llm-backend",
                "local",
                "--llm-base-url",
                "http://localhost:11434/v1",
                "--llm-model",
                "qwen2.5:14b",
            ],
        ),
        patch("saka_cli.cli.run_kb_command") as mock_run_kb,
        patch("saka_cli.cli.suggest_llm_backend") as mock_suggest,
    ):
        main()

    mock_run_kb.assert_called_once()
    kwargs = mock_run_kb.call_args.kwargs
    assert kwargs["llm_backend"] == "local"
    assert kwargs["llm_base_url"] == "http://localhost:11434/v1"
    assert kwargs["llm_model"] == "qwen2.5:14b"
    # An explicit --llm-backend means no hardware hint is needed.
    mock_suggest.assert_not_called()


def test_kb_harness_passes_call_names_flag_through_to_run_kb_command():
    """--kb-call-names PATH is parsed and threaded to `run_kb_command` as `call_names_file`
    (mirrors how --llm-backend/--llm-base-url/--llm-model reach `run_kb_command` unchanged)."""
    with (
        patch(
            "sys.argv",
            [
                "saka-cli",
                "--service",
                "hinatazaka46",
                "--kb-index",
                "--kb-members",
                "members_dir",
                "--kb-call-names",
                "call_names.json",
            ],
        ),
        patch("saka_cli.cli.run_kb_command") as mock_run_kb,
        patch("saka_cli.cli.suggest_llm_backend") as mock_suggest,
    ):
        main()

    mock_run_kb.assert_called_once()
    kwargs = mock_run_kb.call_args.kwargs
    assert kwargs["call_names_file"] == Path("call_names.json")
    mock_suggest.assert_called_once()


def test_kb_harness_leaves_call_names_file_none_when_flag_absent():
    """No --kb-call-names: `run_kb_command` receives `call_names_file=None` -- unchanged
    default behavior for callers that never touch this flag."""
    with (
        patch(
            "sys.argv",
            [
                "saka-cli",
                "--service",
                "hinatazaka46",
                "--kb-index",
                "--kb-members",
                "members_dir",
            ],
        ),
        patch("saka_cli.cli.run_kb_command") as mock_run_kb,
        patch("saka_cli.cli.suggest_llm_backend"),
    ):
        main()

    kwargs = mock_run_kb.call_args.kwargs
    assert kwargs["call_names_file"] is None


def test_kb_harness_logs_hardware_hint_when_backend_unspecified():
    """No --llm-backend: the hardware suggestion is logged as an info hint before running.

    See `test_hw_suggest_prints_hardware_and_suggestion_then_exits` for why this patches
    `structlog.get_logger` rather than `saka_cli.cli.logger` directly.
    """
    fake_suggestion = {
        "recommended": "cloud",
        "local_model": None,
        "tier": "cloud",
        "reason": "no gpu",
    }

    with (
        patch(
            "sys.argv",
            [
                "saka-cli",
                "--service",
                "hinatazaka46",
                "--kb-index",
                "--kb-members",
                "members_dir",
            ],
        ),
        patch("saka_cli.cli.run_kb_command") as mock_run_kb,
        patch(
            "saka_cli.cli.suggest_llm_backend", return_value=fake_suggestion
        ) as mock_suggest,
        patch("saka_cli.cli.structlog.get_logger") as mock_get_logger,
    ):
        main()

    mock_suggest.assert_called_once_with()
    mock_run_kb.assert_called_once()
    assert mock_run_kb.call_args.kwargs["llm_backend"] is None
    mock_logger = mock_get_logger.return_value
    hint_calls = [call.args[0] for call in mock_logger.info.call_args_list]
    assert any("cloud" in msg for msg in hint_calls)
