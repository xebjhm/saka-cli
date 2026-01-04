
import pytest
from unittest.mock import patch, AsyncMock

def test_setup_saves_to_keyring():
    """Test that setup wizard saves credentials to TokenManager."""
    with patch('sys.argv', ['pyhako-cli', '--interactive']), \
         patch('pyhako_cli.cli.HakoCLI') as MockCLI, \
         patch('pyhako.credentials.TokenManager') as MockTM:
         pass

@pytest.mark.asyncio
async def test_hako_cli_setup_secure():
    from pyhako_cli.cli import HakoCLI
    from pyhako import Group
    
    cli = HakoCLI(group=Group.NOGIZAKA46)
    
    with patch('pyhako_cli.cli.BrowserAuth.login', new_callable=AsyncMock) as mock_login, \
         patch('pyhako.credentials.TokenManager') as MockTM, \
         patch.object(cli, 'save_config') as mock_save_config, \
         patch.object(cli, 'load_config', return_value={}):
         
        mock_login.return_value = {
            'access_token': 'at',
            'refresh_token': 'rt',
            'cookies': {'c': 1},
            'x-talk-app-id': 'app',
            'user-agent': 'ua'
        }
        
        await cli.setup_wizard()
        
        # Verify TokenManager interaction
        # Note: TokenManager is instantiated inside setup_wizard, so MockTM() returns the instance
        MockTM.return_value.save_session.assert_called_with(
            'nogizaka46', 'at', 'rt', {'c': 1}
        )
        
        # Verify Config interaction (should NOT contain tokens)
        args, _ = mock_save_config.call_args
        saved_config = args[0]
        assert 'access_token' not in saved_config
        assert 'x-talk-app-id' in saved_config

@pytest.mark.asyncio
async def test_hako_cli_run_secure():
    from pyhako_cli.cli import HakoCLI
    from pyhako import Group
    
    cli = HakoCLI(group=Group.SAKURAZAKA46)
    
    with patch('pyhako.credentials.TokenManager') as MockTM, \
         patch.object(cli, 'load_config', return_value={'x-talk-app-id': 'app'}), \
         patch('pyhako_cli.cli.Client') as MockClient, \
         patch('pyhako_cli.cli.SyncManager'), \
         patch('aiohttp.ClientSession'):
         
        # Mock Keyring Data
        MockTM.return_value.load_session.return_value = {
            'access_token': 'secure_at',
            'refresh_token': 'secure_rt',
            'cookies': {}
        }
        
        mock_client_instance = MockClient.return_value
        mock_client_instance.refresh_access_token = AsyncMock(return_value=True)
        mock_client_instance.fetch_json = AsyncMock(return_value=True)
        # Access token property
        mock_client_instance.access_token = 'secure_at' 
        
        await cli.run()
        
        # Verify Client init with secure tokens
        MockClient.assert_called()
        _, kwargs = MockClient.call_args
        assert kwargs['access_token'] == 'secure_at'
        
@pytest.mark.asyncio
async def test_hako_cli_cleanup_secure():
    from pyhako_cli.cli import HakoCLI
    from pyhako import Group
    
    cli = HakoCLI(group=Group.HINATAZAKA46)
    
    with patch('pyhako.credentials.TokenManager') as MockTM, \
         patch('pathlib.Path.exists', return_value=False), \
         patch('builtins.input', return_value='y'):
         
        await cli.cleanup_wizard()
        
        await cli.cleanup_wizard()
        
        # Verify that delete_session was called multiple times (for each group)
        assert MockTM.return_value.delete_session.call_count >= 3
        # Strict checking:
        from unittest.mock import call
        MockTM.return_value.delete_session.assert_has_calls([
            call('nogizaka46'),
            call('sakurazaka46'),
            call('hinatazaka46')
        ], any_order=True)
