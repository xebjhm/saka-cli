# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-08-24

### Changed
- **BREAKING**: Message output moved under a per-service folder. Messages now land
  in `{output}/{GroupDisplayName}/messages/...` instead of `{output}/messages/...`,
  and `sync_state.json` moved alongside it. This makes the CLI's output identical
  to SakaDesk's, so both tools can share one output directory. Existing trees need
  their `messages/` and `sync_state.json` moved into the group folder.
- **BREAKING**: Blogs are now saved as `blog.json` (the format SakaDesk reads)
  rather than `index.html`. Pass `--html` to also write a readable `index.html`
  next to each `blog.json`.
- Blog backup is now two-stage like the GUI: a fast list-page scan builds
  `blogs/index.json`, then only uncached posts have their detail pages fetched.
  Re-running no longer re-downloads everything.
- Message sync fetches each group's timeline once and distributes it to that
  group's members in memory, instead of one API call per member.
- Requires Python 3.10+ and pysaka >= 0.3.0.

### Fixed
- `-m/--members` no longer coerces IDs to integers. Blog member IDs are site
  codes (Nogizaka's are zero-padded), so the old `int()` parsing either raised
  or silently changed the ID and selected the wrong member.

### Added
- `sync_metadata.json` per service, with the group/member index, thumbnails and
  per-member cursors SakaDesk's chat list reads.
- `blogs/member_thumbnails/` with `members_cache.json`, content-hashed so repeat
  runs skip re-downloading portraits.
- Media dimensions, duration and mute flags are extracted and written back into
  `messages.json` after download.
- `--html` flag for a readable `index.html` alongside each blog.
- `--initial-limit` to cap how much history a member's first sync pulls
  (defaults to 1000, matching SakaDesk).
- Blogs removed upstream (404/410) are flagged in `index.json` instead of being
  retried on every run.

## [0.1.1] - 2026-01-11

### Added
- Blog backup support (`--blog` flag)
- Improved error messages for authentication failures
- Git Flow workflow documentation (CONTRIBUTING.md, PR template)

### Changed
- Updated to use pysaka TokenManager for credential storage
- CI now uses PyPI pysaka instead of local checkout
- Requires pysaka >=0.1.1

### Fixed
- Session expiration handling with proper re-login prompt

## [0.1.0] - 2026-01-11

### Added
- Initial pysaka CLI release
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

[Unreleased]: https://github.com/xebjhm/saka-cli/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/xebjhm/saka-cli/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/xebjhm/saka-cli/releases/tag/v0.1.0
