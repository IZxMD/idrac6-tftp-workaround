# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-07-19

Second external-review pass. The four highest-priority robustness points were
implemented; SSH host-key pinning was considered and deliberately left out as
out of scope for the trusted-management-network model this tool assumes (the
README security note now says so explicitly rather than calling it a hard
limit).

### Added
- `--force` flag on `idrac_flash.py` (1.4.0). A non-zero update semaphore now
  aborts the flash by default instead of only warning; `--force` downgrades it
  back to a warning and proceeds. A stale lock after an aborted TFTP attempt
  is the usual reason you'd need it.
- New mock scenarios and test cases: truncated upload, non-zero semaphore
  (with and without `--force`) in `tests/run_test.sh`, and slow blockwise SSH
  output in `tests/run_test_backup.sh`.

### Fixed
- Upload completion is now verified numerically: the `<receivedBytes>` value
  is parsed and the flash aborts if it's smaller than the image sent, instead
  of only checking that the field is present. This is the exact truncation
  failure mode (a cut-short transfer) that the whole tool exists to route
  around, so a short upload must never be flashed.
- The HTTP client no longer treats every receive error as a clean
  end-of-response. A `ConnectionResetError` is surfaced (so e.g. a
  half-received flash-commit reply can't be parsed as a success); the ancient
  TLS 1.0 stack's unclean close (unexpected-EOF `SSLError`) is still accepted
  as EOF, since on this hardware that is the normal end of a response.
- `idrac_backup_config.py` (1.0.2): the racadm-shell reader now ends on the
  shell prompt (primary) with a longer quiet-period fallback, so a slow iDRAC
  pausing mid-output no longer silently truncates a config group. This also
  speeds up the common case (no more waiting out the settle window per
  command).
- The flash log and the config backup file are created with `0600`
  (owner-only) permissions on POSIX, since they can contain internal iDRAC
  state and full network/user/SNMP/IPMI configuration. Harmless no-op on
  Windows.

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
