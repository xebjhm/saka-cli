# PyHako CLI

> Seamlessly scrape, archive, and manage your Sakamichi Series messages.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Features
- **Multi-Group Support**: Nogizaka46, Hinatazaka46, and Sakurazaka46.
- **Resilient**: Auto-retry, persistent sessions, and robust error handling.
- **Secure**: Local token storage (System Keyring).

## Installation

### 1. Download Binary
Download the latest executable for your operating system from the **[Releases Page](../../releases)**.
- **Windows**: `pyhako-cli-windows.exe`
- **Linux**: `pyhako-cli-linux`

### 2. Run
No installation required. Just double-click the file or run it from your terminal.

## Quick Start
New to CLI tools? Just run the executable without arguments to start the **Interactive Wizard**:

```bash
# Windows
.\pyhako-cli-windows.exe

# Linux
./pyhako-cli-linux
```

The wizard will guide you through:
1.  Selecting your Service (e.g., Nogizaka46).
2.  Logging in via Browser.
3.  Choosing download locations.

## Advanced Usage (Batch Mode)
For power users and automation (CI/CD, Cron).

### 1. Setup & Authentication
Perform a one-time login for a specific group.
```bash
# Setup for Nogizaka46 (Default)
./pyhako-cli -s nogizaka46 --interactive

# Setup for Hinatazaka46
./pyhako-cli -s hinatazaka46 --interactive
```

### 2. Scraping
**Basic Sync (All subscribed members):**
```bash
./pyhako-cli -s nogizaka46
```

**Include Expired/Inactive Members:**
```bash
./pyhako-cli -s nogizaka46 --include-offline
```

**Target Specific Members:**
```bash
# Sync specific Group ID and Member IDs
./pyhako-cli -g 12 -m 34 56
```

### 3. Cleanup
Remove all local authentication data and tokens.
```bash
./pyhako-cli --cleanup
```

## Configuration
Configuration is stored in `config_{group}.json` in the current working directory.
- `auth_data/`: Browser context and cookies.
- `output/`: Downloaded images, videos, and voice messages.

## Linux Support & Troubleshooting

PyHakoCLI uses the system keyring to securely store authentication tokens.

### 1. Standard Desktop (GNOME/KDE)
Ensure `gnome-keyring` is installed and unlocked:
```bash
sudo apt-get install gnome-keyring
```

### 2. Headless / Server / WSL
If you see **"Prompt dismissed"** or **"No recommended backend"** errors, your environment lacks a GUI prompt to unlock the keyring.

**Solution**: Install `keyring` and `keyrings.alt` locally if using the Python version, OR if using the binary, it should handle fallback automatically if built correctly. 

*Note: The CLI attempts to auto-detect this and fallback to a file-based keyring.*

## License
MIT

---
**For Developers:** check [DEVELOPMENT.md](DEVELOPMENT.md) for build instructions.
