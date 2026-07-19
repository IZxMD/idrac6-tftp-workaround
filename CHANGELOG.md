# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-07-19

### Added
- `--backup` flag on `idrac_flash.py`: optionally shells out to
  `idrac_backup_config.py` as a subprocess before flashing, using the same
  envfile. Voluntary and a subprocess call rather than an import, so
  `idrac_flash.py` still has no third-party dependency of its own; only
  `idrac_backup_config.py` needs `paramiko`, and only when `--backup` is
  actually used. If the backup fails, the flash is aborted with its error
  message.
- `tests/run_test.sh`: a combined-mocks case exercising `--backup` end to
  end (mock HTTPS iDRAC + mock SSH/racadm shell together).

## [1.2.0] - 2026-07-18

Adds an optional pre-flash config backup, prompted by a Reddit comment
pointing out that `preConfig=on` preserves the config through the flash, but
that's not the same thing as a restorable backup.

### Added
- `idrac_backup_config.py` (1.0.1): a separate, optional script that reads
  the current iDRAC config over SSH (`racadm getconfig -g <group>` for
  network, users, RAC security tuning, serial/console, SNMP and IPMI
  settings) and saves it to a local text file before you flash anything.
  This is the only script in the repo with a third-party dependency
  (`paramiko==2.11.0` specifically; newer versions fail against iDRAC6's old
  SSH algorithms). `cfgUserAdmin` is read per user slot (`-i 1` through
  `-i 16`), since real iDRAC6 hardware treats it as an indexed group and
  rejects a plain `-g cfgUserAdmin` call with "ERROR: The indexed group
  specified requires -i <index>" (confirmed against real hardware, not the
  mock). Warns by group name if a group comes back empty or errors, in case
  your firmware revision uses different group names.
- `tests/mock_ssh_idrac.py` + `tests/run_test_backup.sh`: the same
  no-hardware-needed testing approach as the flash script, against a mock
  SSH/racadm shell instead of a mock web server, including a scenario for
  the indexed `cfgUserAdmin` behavior above.
- `--version` flag on `idrac_backup_config.py`.

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
