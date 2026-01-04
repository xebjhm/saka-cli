import pytest
from unittest.mock import patch, AsyncMock
from pyhako_cli.cli import main

@pytest.fixture(autouse=True)
def mock_dependencies():
    with patch('pyhako_cli.cli.setup_logging'), \
         patch('pyhako_cli.cli.Client'), \
         patch('pyhako_cli.cli.SyncManager'), \
         patch('pyhako_cli.cli.BrowserAuth'):
        yield

def test_cli_help():
    """Test that the CLI can run --help without error."""
    with patch('sys.argv', ['pyhako-cli', '--help']):
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0

def test_cli_no_args_triggers_interactive():
    """Test that no args results in interactive mode."""
    with patch('sys.argv', ['pyhako-cli']), \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI:
        
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()
        mock_instance.run_interactive_wizard.return_value = {
            'service': 'hinatazaka46'
        }
        
        main()
        
        # Should have called interactive wizard
        mock_instance.run_interactive_wizard.assert_called_once()


def test_cli_batch_mode():
    """Test explicit batch mode execution."""
    with patch('sys.argv', ['pyhako-cli', '-o', 'outdir', '-s', 'nogizaka46']), \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI:
        
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()
        
        main()
        
        # Should NOT call wizard
        mock_instance.run_interactive_wizard.assert_not_called()
        
        from pyhako import Group
        # Should initialize CLI with passed args
        MockCLI.assert_called_with(output_dir='outdir', group=Group.NOGIZAKA46)
        # Should run
        mock_instance.run.assert_called_once()

def test_cli_cleanup_flag():
    """Test that --cleanup triggers cleanup_wizard."""
    with patch('sys.argv', ['pyhako-cli', '--cleanup']), \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI:
        
        mock_instance = MockCLI.return_value
        mock_instance.cleanup_wizard = AsyncMock()
        
        main()
        
        mock_instance.cleanup_wizard.assert_called_once()

def test_cli_interactive_flag():
    """Test that --interactive triggers the wizard."""
    with patch('sys.argv', ['pyhako-cli', '--interactive']), \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI:
        
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()
        # Mock the wizard to return specific config
        mock_instance.run_interactive_wizard.return_value = {
            'output_dir': 'custom_out',
            'include_offline': True,
            'group_id': 123,
            'service': 'nogizaka46'
        }
        
        main()
        
        # Should call wizard
        mock_instance.run_interactive_wizard.assert_called_once()
        
        # Should initialize CLI with the wizard's output (and appropriate Group)
        # Note: We need to check if ScraperCLI was initialized with Group.NOGIZAKA46
        # Since we mock ScraperCLI class, we can check call args
        from pyhako import Group
        MockCLI.assert_called_with(output_dir='custom_out', group=Group.NOGIZAKA46)
        
        # Should run with wizard's config
        mock_instance.run.assert_called_once_with(
            group_ids=[123],
            member_ids=None,
            include_inactive=True
        )

def test_cli_lang_arg():
    """Test that --lang flag sets the language."""
    from pyhako_cli import strings
    
    with patch('sys.argv', ['pyhako-cli', '--lang', 'ja']), \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI:
        
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()
        
        main()
        
        # Verify language was set. 
        # Since string module is imported, we can check its internal state or check if get_string returns ja strings.
        assert strings._CURRENT_LANG == 'ja'
        
        # Verify reset for other tests
        strings.set_language('en')

def test_cli_lang_detection():
    """Test that language detection works (mocking locale)."""
    from pyhako_cli import strings
    from pyhako_cli.cli import detect_system_language
    
    # Test Japanese detection
    with patch('locale.setlocale'), \
         patch('locale.getlocale', return_value=('ja_JP', 'UTF-8')), \
         patch('os.environ.get', return_value=None):
        assert detect_system_language() == 'ja'
        
    # Test Traditional Chinese
    with patch('locale.setlocale'), \
         patch('locale.getlocale', return_value=('zh_TW', 'UTF-8')), \
         patch('os.environ.get', return_value=None):
        assert detect_system_language() == 'zh-TW'
        
    # Test Simplified Chinese (fallback)
    with patch('locale.setlocale'), \
         patch('locale.getlocale', return_value=('zh_CN', 'UTF-8')), \
         patch('os.environ.get', return_value=None):
        assert detect_system_language() == 'zh-CN'


def test_cli_full_batch_mode():
    """Test batch mode with ALL optional arguments (group, members, offline)."""
    # Args: -o out -s sakurazaka46 -g 100 -m 1 2 3 --include-offline
    argv = [
        'pyhako-cli', 
        '-o', 'custom_out', 
        '-s', 'sakurazaka46', 
        '-g', '100', 
        '-m', '1', '2', '3', 
        '--include-offline'
    ]
    
    with patch('sys.argv', argv), \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI:
        
        mock_instance = MockCLI.return_value
        mock_instance.run = AsyncMock()
        
        main()
        
        from pyhako import Group
        # Check Init
        MockCLI.assert_called_with(output_dir='custom_out', group=Group.SAKURAZAKA46)
        
        # Check Run Args
        mock_instance.run.assert_called_once_with(
            group_ids=[100],
            member_ids=[1, 2, 3],
            include_inactive=True
        )

def test_cli_verbose():
    """Test that -v flag enables verbose logging."""
    with patch('sys.argv', ['pyhako-cli', '-v']), \
         patch('pyhako_cli.cli.setup_logging') as mock_setup, \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI: # Mock CLI to prevent run
         
        # We need to mock instance methods to avoid main() crashing after setup_logging
        MockCLI.return_value.run = AsyncMock()
        MockCLI.return_value.run_interactive_wizard.return_value = {} 
        
        main()
        
        mock_setup.assert_called_with(verbose=True)

def test_interactive_wizard_skips_lang_prompt_when_detected():
    """Test that interactive wizard skips lang prompt when language was pre-detected (non-English)."""
    from pyhako_cli.cli import HakoCLI
    from pyhako_cli import strings
    
    # Set language to Japanese (simulating auto-detection from system/config)
    original_lang = strings.get_language()
    strings.set_language('ja')
    
    try:
        cli = HakoCLI()
        
        # Mock input to track what prompts are shown
        inputs_given = []
        def mock_input(prompt=""):
            inputs_given.append(prompt)
            return ""  # Default for all prompts
        
        with patch('builtins.input', side_effect=mock_input), \
             patch('builtins.print'):
            config = cli.run_interactive_wizard()
        
        # Should NOT prompt for language (skipped), so fewer inputs than when English
        # Language should be preserved as 'ja'
        assert config.get('lang') == 'ja'
    finally:
        # Reset language
        strings.set_language(original_lang)

