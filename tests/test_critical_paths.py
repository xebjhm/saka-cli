"""Critical path tests for saka-cli.

Covers: ToS acceptance/rejection, blog backup flag, invalid service handling,
language detection edge cases, parse_int_list validation, cleanup wizard
token deletion, and string translation completeness.
"""

import pytest
from unittest.mock import patch
from pysaka import Group

from saka_cli.cli import (
    SakaCLI,
    get_parser,
    detect_system_language,
    parse_int_list,
)
from saka_cli.strings import STRINGS


# ---------------------------------------------------------------------------
# 1. ToS acceptance / rejection
# ---------------------------------------------------------------------------


class TestCheckTos:
    """Tests for SakaCLI.check_tos() covering accept, reject, and pre-agreed."""

    def test_tos_accept_saves_config(self, tmp_path):
        """When user inputs 'y', config is saved with tos_agreed=True and returns True."""
        cli = SakaCLI(group=Group.NOGIZAKA46)
        cli.config_file = tmp_path / "config_nogizaka46.json"

        with (
            patch.object(cli, "load_config", return_value={}),
            patch.object(cli, "save_config") as mock_save,
            patch("builtins.input", return_value="y"),
            patch("builtins.print"),
        ):
            result = cli.check_tos()

        assert result is True
        mock_save.assert_called_once()
        saved_config = mock_save.call_args[0][0]
        assert saved_config["tos_agreed"] is True

    def test_tos_reject_returns_false(self, tmp_path):
        """When user inputs 'n', returns False without saving tos_agreed."""
        cli = SakaCLI(group=Group.SAKURAZAKA46)
        cli.config_file = tmp_path / "config_sakurazaka46.json"

        with (
            patch.object(cli, "load_config", return_value={}),
            patch.object(cli, "save_config") as mock_save,
            patch("builtins.input", return_value="n"),
            patch("builtins.print"),
        ):
            result = cli.check_tos()

        assert result is False
        mock_save.assert_not_called()

    def test_tos_empty_input_returns_false(self, tmp_path):
        """When user inputs empty string (just presses Enter), returns False."""
        cli = SakaCLI(group=Group.HINATAZAKA46)
        cli.config_file = tmp_path / "config_hinatazaka46.json"

        with (
            patch.object(cli, "load_config", return_value={}),
            patch.object(cli, "save_config") as mock_save,
            patch("builtins.input", return_value=""),
            patch("builtins.print"),
        ):
            result = cli.check_tos()

        assert result is False
        mock_save.assert_not_called()

    def test_tos_already_agreed_skips_prompt(self, tmp_path):
        """When config already has tos_agreed=True, skips prompt and returns True."""
        cli = SakaCLI(group=Group.NOGIZAKA46)
        cli.config_file = tmp_path / "config_nogizaka46.json"

        with (
            patch.object(cli, "load_config", return_value={"tos_agreed": True}),
            patch("builtins.input") as mock_input,
            patch("builtins.print"),
        ):
            result = cli.check_tos()

        assert result is True
        mock_input.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Blog backup mode flag
# ---------------------------------------------------------------------------


class TestBlogFlag:
    """Tests for the --blog argument flag in get_parser()."""

    def test_blog_flag_parsed(self):
        """--blog flag is properly parsed and sets args.blog = True."""
        parser = get_parser()
        args = parser.parse_args(["--blog", "-s", "nogizaka46"])
        assert args.blog is True

    def test_blog_flag_default_false(self):
        """Without --blog flag, args.blog defaults to False."""
        parser = get_parser()
        args = parser.parse_args(["-s", "nogizaka46"])
        assert args.blog is False


# ---------------------------------------------------------------------------
# 3. Invalid group / service handling
# ---------------------------------------------------------------------------


class TestInvalidServiceHandling:
    """Tests for invalid and valid --service argument values."""

    def test_invalid_service_raises_system_exit(self):
        """An invalid service name causes argparse to raise SystemExit."""
        parser = get_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--service", "foobar"])

    def test_valid_service_nogizaka46(self):
        """Valid service 'nogizaka46' is accepted."""
        parser = get_parser()
        args = parser.parse_args(["-s", "nogizaka46"])
        assert args.service == "nogizaka46"

    def test_valid_service_sakurazaka46(self):
        """Valid service 'sakurazaka46' is accepted."""
        parser = get_parser()
        args = parser.parse_args(["-s", "sakurazaka46"])
        assert args.service == "sakurazaka46"

    def test_valid_service_hinatazaka46(self):
        """Valid service 'hinatazaka46' is accepted."""
        parser = get_parser()
        args = parser.parse_args(["-s", "hinatazaka46"])
        assert args.service == "hinatazaka46"


# ---------------------------------------------------------------------------
# 4. Language detection edge cases
# ---------------------------------------------------------------------------


class TestDetectSystemLanguage:
    """Tests for detect_system_language() with various locale strings."""

    def test_en_us_utf8(self):
        """en_US.UTF-8 falls through to default 'en'."""
        with (
            patch("os.environ.get", return_value=None),
            patch("locale.setlocale"),
            patch("locale.getlocale", return_value=("en_US", "UTF-8")),
        ):
            assert detect_system_language() == "en"

    def test_ja_jp_utf8(self):
        """ja_JP.UTF-8 is detected as 'ja'."""
        with (
            patch("os.environ.get", return_value=None),
            patch("locale.setlocale"),
            patch("locale.getlocale", return_value=("ja_JP", "UTF-8")),
        ):
            assert detect_system_language() == "ja"

    def test_zh_tw_utf8(self):
        """zh_TW.UTF-8 is detected as 'zh-TW'."""
        with (
            patch("os.environ.get", return_value=None),
            patch("locale.setlocale"),
            patch("locale.getlocale", return_value=("zh_TW", "UTF-8")),
        ):
            assert detect_system_language() == "zh-TW"

    def test_zh_cn_utf8(self):
        """zh_CN.UTF-8 is detected as 'zh-CN'."""
        with (
            patch("os.environ.get", return_value=None),
            patch("locale.setlocale"),
            patch("locale.getlocale", return_value=("zh_CN", "UTF-8")),
        ):
            assert detect_system_language() == "zh-CN"

    def test_unknown_locale_defaults_to_en(self):
        """An unrecognized locale falls back to 'en'."""
        with (
            patch("os.environ.get", return_value=None),
            patch("locale.setlocale"),
            patch("locale.getlocale", return_value=("ko_KR", "UTF-8")),
        ):
            assert detect_system_language() == "en"

    def test_env_lang_takes_priority(self):
        """LANG environment variable takes priority over getlocale()."""
        with patch(
            "os.environ.get",
            side_effect=lambda k, *a: "ja_JP.UTF-8" if k == "LANG" else None,
        ):
            assert detect_system_language() == "ja"

    def test_none_locale_defaults_to_en(self):
        """When getlocale() returns (None, None), defaults to 'en'."""
        with (
            patch("os.environ.get", return_value=None),
            patch("locale.setlocale"),
            patch("locale.getlocale", return_value=(None, None)),
        ):
            assert detect_system_language() == "en"


# ---------------------------------------------------------------------------
# 5. parse_int_list validation
# ---------------------------------------------------------------------------


class TestParseIntList:
    """Tests for the parse_int_list() utility function."""

    def test_comma_separated(self):
        """'1,2,3' parses to [1, 2, 3]."""
        assert parse_int_list("1,2,3") == [1, 2, 3]

    def test_comma_separated_with_spaces(self):
        """'1, 2, 3' (commas with spaces) parses correctly."""
        assert parse_int_list("1, 2, 3") == [1, 2, 3]

    def test_single_value(self):
        """'42' parses to [42]."""
        assert parse_int_list("42") == [42]

    def test_invalid_letters_raises_value_error(self):
        """Non-numeric input raises ValueError."""
        with pytest.raises(ValueError):
            parse_int_list("abc")

    def test_mixed_invalid_raises_value_error(self):
        """Mixed numeric and non-numeric input with commas raises ValueError."""
        with pytest.raises(ValueError):
            parse_int_list("1,abc,3")

    def test_empty_string_raises(self):
        """Empty string raises ValueError (cannot convert '' to int)."""
        with pytest.raises(ValueError):
            parse_int_list("")


# ---------------------------------------------------------------------------
# 6. Cleanup wizard deletes ALL Group enum values
# ---------------------------------------------------------------------------


class TestCleanupWizardDeletesAllGroups:
    """Tests that cleanup_wizard() calls delete_session for every Group value."""

    @pytest.mark.asyncio
    async def test_cleanup_deletes_all_group_sessions(self):
        """cleanup_wizard() iterates over all Group enum values and calls delete_session for each."""
        cli = SakaCLI(group=Group.NOGIZAKA46)

        with (
            patch("pysaka.credentials.TokenManager") as MockTM,
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.glob", return_value=[]),
            patch("builtins.input", return_value="y"),
            patch("builtins.print"),
            patch("saka_cli.cli.logger"),
        ):
            await cli.cleanup_wizard()

            # Collect all delete_session calls
            delete_calls = MockTM.return_value.delete_session.call_args_list
            deleted_services = [c.args[0] for c in delete_calls]

            # Every Group enum value must have been passed to delete_session
            for g in Group:
                assert g.value in deleted_services, (
                    f"delete_session was not called for Group.{g.name} ('{g.value}')"
                )

    @pytest.mark.asyncio
    async def test_cleanup_aborted_when_user_declines(self):
        """When user declines the confirmation prompt, no sessions are deleted."""
        cli = SakaCLI(group=Group.NOGIZAKA46)

        with (
            patch("pysaka.credentials.TokenManager") as MockTM,
            patch("builtins.input", return_value="n"),
            patch("builtins.print"),
        ):
            # cleanup_wizard uses logger.info which requires logger to be set
            with patch("saka_cli.cli.logger"):
                await cli.cleanup_wizard()

            MockTM.return_value.delete_session.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Strings coverage - all languages have the same set of keys
# ---------------------------------------------------------------------------


class TestStringsCompleteness:
    """Tests that all language dictionaries have identical key sets."""

    def test_all_languages_have_same_keys(self):
        """Every language in STRINGS must define exactly the same set of keys as English."""
        en_keys = set(STRINGS["en"].keys())

        for lang, lang_dict in STRINGS.items():
            lang_keys = set(lang_dict.keys())
            missing = en_keys - lang_keys
            extra = lang_keys - en_keys

            assert not missing, (
                f"Language '{lang}' is missing keys present in 'en': {sorted(missing)}"
            )
            assert not extra, (
                f"Language '{lang}' has extra keys not in 'en': {sorted(extra)}"
            )

    def test_all_five_languages_present(self):
        """STRINGS contains exactly the expected 5 languages."""
        expected = {"en", "ja", "zh-TW", "zh-CN", "yue"}
        actual = set(STRINGS.keys())
        assert actual == expected, (
            f"Expected languages {sorted(expected)}, got {sorted(actual)}"
        )

    def test_no_empty_string_values(self):
        """No string value should be empty across any language."""
        for lang, lang_dict in STRINGS.items():
            for key, value in lang_dict.items():
                assert value, f"Language '{lang}', key '{key}' has an empty/falsy value"
