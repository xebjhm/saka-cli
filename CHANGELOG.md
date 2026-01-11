# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-01-11

### Added
- Blog backup support (`--blog` flag)
- Improved error messages for authentication failures
- Git Flow workflow documentation (CONTRIBUTING.md, PR template)

### Changed
- Updated to use pyhako TokenManager for credential storage
- CI now uses PyPI pyhako instead of local checkout
- Requires pyhako >=0.1.1

### Fixed
- Session expiration handling with proper re-login prompt

## [0.1.0] - 2026-01-11

### Added
- Initial PyHako CLI release
- Interactive group selection (Hinatazaka46, Nogizaka46, Sakurazaka46)
- Browser-based OAuth authentication
- Full message synchronization with progress display
- Incremental sync support
- Rich terminal output with progress bars
- PyInstaller packaging for standalone executable
- Cross-platform support (Windows, Linux, macOS)

### Security
- Secure token storage via system keyring
- No plaintext credential storage

[Unreleased]: https://github.com/xebjhm/PyHakoCLI/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/xebjhm/PyHakoCLI/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/xebjhm/PyHakoCLI/releases/tag/v0.1.0
