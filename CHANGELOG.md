# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `.github/ISSUE_TEMPLATE/bug_report.md`: asks for `--check` output, card
  type (Enterprise/Express) and firmware revision, since only an Enterprise
  card has been tested so far and coverage on other revisions depends on
  bug reports carrying the right diagnostics.
- `SECURITY.md`: how to report a real vulnerability, and which files (config
  backups) are sensitive and shouldn't be posted publicly. `idrac_flash_log.txt`
  is explicitly called out as safe to attach, it never logs the password or
  session cookie/token.

### Removed
- `idrac_web_explore.py`. Deprecated and unused: `tests/capture_idrac.py`
  covers the same job (recording how your iDRAC answers the endpoints
  `idrac_flash.py` uses) more thoroughly, and is what the README already
  points to for that purpose.

## [1.5.0] - 2026-07-19

Real-hardware reverse-engineering pass. Captured the actual on-the-wire
behavior of a live iDRAC6 (login/data traffic and racadm shell output) and
made the tool and the mocks match reality instead of assumptions. This
surfaced two real gaps the old mocks had hidden.

### Added
- `idrac_flash.py` (1.5.0) now speaks both iDRAC web-API dialects: the newer
  2.x one requires the `ST2` session token (from the login response's
  forwardUrl) as an HTTP header on authenticated `/data` requests and reports
  the running version as `fwVersion`; the older 1.9x accepted the cookie
  alone and used `spfwVer`. The client detects and uses whichever is present,
  so it works across firmware revisions.
- `--check` flag on `idrac_flash.py`: a read-only preflight that logs in,
  reads the session token, running version and update semaphore, prints them
  and exits without uploading or flashing. Safe to run against a live iDRAC;
  verified against real 2.92 hardware.
- `tests/capture_idrac.py`: a read-only diagnostic that records how your own
  iDRAC answers the endpoints this tool uses, so you can check the mock (and
  the parser) against your firmware revision.
- New mock scenarios: `noauth` (models 1.9x cookie-only firmware) in the web
  mock; the racadm mock now echoes commands, uses the real `/admin1->` prompt
  and `\r\r\n` line endings.

### Fixed
- Post-flash verification and reboot detection, both found flashing real
  hardware end to end (1.98 -> 2.85 -> 2.92). The client no longer trusts the
  first successful reconnect: during the flash write the web interface can
  briefly drop and return still on the OLD version a minute before the real
  firmware-switch reboot, which made the tool report the old version as final.
  It now polls until the running version actually reflects the new firmware.
  That same loop is the verification: when the firmware reports no staged
  target version (real 2.85/2.92 leave spfwVer empty), success is the running
  version changing from before the flash; a version that stays unchanged after
  the full reboot window is now a clear FAILED instead of a vague UNVERIFIED.
  New `notarget` mock scenario covers the empty-staged-version path.
- The firmware upload itself, which failed against 2.x with the ST-token auth
  fixed. Three separate real problems, all found by capturing live traffic:
  (1) the upload POST needs the session token as a `?ST1=<st1>` query param
  (a plain form POST can't set the ST2 header); without it the iDRAC 302s to
  start.html. (2) With that fixed, a full image failed with HTTP 500 at ~6 MB:
  the short multipart boundary made the iDRAC's Appweb buffer the body against
  its 6 MB request-body limit. A captured real browser upload sent the same
  55 MB image, same `?ST1=` URL and same `Content-Length` and got 200, the
  only pre-failure difference being a long Gecko-style boundary (long dash run
  + digits) that routes the request to Appweb's streaming upload handler. The
  client now uses that boundary format. (3) preConfig is now sent present but
  empty (value ""), matching the real form's checkbox, instead of "on".
- Auth against 2.x firmware: the client previously sent only the session
  cookie and would get 401 on every authenticated request against firmware
  that requires the ST2 token. It now extracts and sends ST2.
- Version detection against 2.x firmware: the running version is read from
  `fwVersion` (with `spfwVer` fallback) instead of `spfwVer` alone, which is
  empty on newer firmware.
- `idrac_backup_config.py` (1.0.3): empty-group detection now strips the
  echoed command and the shell prompt from each command's output before
  deciding a group is empty. Real racadm output always includes those two
  lines, so the previous check would never have flagged a genuinely empty
  group on real hardware. Config backups are also cleaner (no shell noise).
- The web mock now reflects real behavior: responses wrapped as
  `<root>...<status>ok</status></root>`, the `Mbedthis-Appweb/2.4.2` server
  header, login returning an ST1/ST2 forwardUrl, and ST2 required on
  authenticated requests, so the tests actually exercise the real auth path.

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
