# PyHako CLI

> Seamlessly scrape, archive, and manage your Sakamichi Series messages.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Disclaimer & Warnings

> [!CAUTION]
> **Use at your own risk.**
> By using this software, you acknowledge and agree to the Terms of Service below.
> **Automated Usage:** If you run this tool in an automated environment (CI/CD, Scripts), you are implicitly agreeing to these terms.

### 規約 / Terms of Service Ref.

The following are excerpts from the official Terms of Service. Please refer to the full text here:
- [Nogizaka46](https://contact.nogizaka46.com/s/n46app/page/app_terms)
- [Sakurazaka46](https://sakurazaka46.com/s/s46app/page/app_terms)
- [Hinatazaka46](https://www.hinatazaka46.com/s/h46app/page/app_terms)

**第3条（知的財産権）/ Article 3 (Intellectual Property)**
> 3. 当社が別に定める場合を除き、お客様が本コンテンツを複製、翻案、頒布、公衆送信等することは禁止します。

**第8条（禁止事項）/ Article 8 (Prohibited Acts)**
> (16) 当社が指定するアクセス方法以外の手段で本サービスにアクセスし、またはアクセスを試みる行為

> (17) 自動化された手段（クローラおよび類似の技術を含む）を用いて本サービスにアクセスし、またはアクセスを試みる行為


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
./pyhako-cli-linux -s nogizaka46 --interactive
# OR (Windows)
.\pyhako-cli-windows.exe -s nogizaka46 --interactive

# Setup for Hinatazaka46
./pyhako-cli-linux -s hinatazaka46 --interactive
```

### 2. Scraping
**Basic Sync (All subscribed members):**
```bash
./pyhako-cli-linux -s nogizaka46
```

**Include Expired/Inactive Members:**
```bash
./pyhako-cli-linux -s nogizaka46 --include-offline
```

**Target Specific Members:**
```bash
# Sync specific Group ID and Member IDs
./pyhako-cli-linux -g 12 -m 34 56
```

### 3. Cleanup
Remove all local authentication data and tokens.
```bash
./pyhako-cli-linux --cleanup
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
