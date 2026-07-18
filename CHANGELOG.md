# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-07-18

Robustness and verification pass, based on an external code review. The flash
flow is unchanged; what changed is how carefully each step is checked so a
failed flash can no longer be reported as a success.

### Added
- Real firmware verification: the script now records the running version
  before the flash, reads the target version from the staged image, and
  compares the post-reboot version against that target. It exits `0` only on
  a verified match and reports MISMATCH/UNVERIFIED otherwise.
- SHA-256 of the `.d6` image is printed and logged before upload, so it can
  be checked against Dell's published checksum.
- HTTP status codes are parsed and the flash-commit responses
  (`fwUpdateState:4`, `fwUpdate:1`) are validated; a rejected commit now
  aborts instead of printing "flashing in progress".
- The update semaphore (`fwSemStatus`) is interpreted and warns if another
  update looks to be in progress, instead of only being logged.
- `tests/` ships a mock iDRAC server and `run_test.sh` covering the happy
  path plus rejected-commit, version-mismatch, wrong-password and
  unreachable-host failure cases. No hardware or network needed.
- Optional `port:` key in the env file (default 443) and `IDRAC_*`
  environment overrides for the poll intervals and timeouts.
- `--version` flag and a version banner.

### Fixed
- Upload could in theory skip bytes: `socket.send()` may send fewer bytes
  than requested, and the old loop advanced by the full chunk size
  regardless. It now honors the actual number of bytes sent and raises on a
  closed socket.
- A dropped connection during flashing is no longer unconditionally treated
  as a successful reboot; it requires evidence the flash actually started (or
  repeated failures) first.
- Missing env keys and missing files now produce clear error messages instead
  of a raw `KeyError`/traceback.
- Read timeouts mid-response are surfaced instead of being swallowed as a
  clean end-of-response.
- Corrected a stale docstring in `idrac_web_explore.py` that referenced two
  script names that no longer exist.

### Changed
- The multipart upload body is streamed as separate chunks (header, image,
  trailer) instead of being concatenated into one buffer, so the ~57 MB image
  is not duplicated in memory.
- The expected TLS 1.0 deprecation warning is suppressed to keep output clean.

## [1.0.0] - 2026-07-18

Initial release: single-script iDRAC6 firmware flash over HTTPS/TLS 1.0 as a
workaround for the broken TFTP client in old iDRAC6 firmware, plus the
`idrac_web_explore.py` helper and full root-cause writeup in the README.
