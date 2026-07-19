# iDRAC6 Firmware Update via Web-Upload (TFTP Workaround)

A single script that flashes iDRAC6 firmware over HTTPS/TLS 1.0, bypassing
the broken TFTP client found in old iDRAC6 firmware (observed on 1.98,
likely other early revisions too). No browser, no Java Web Start, no TFTP
server required, and no babysitting either: it logs in, uploads, triggers
the flash, watches it through the reboot, and then verifies the running
firmware actually changed to the version that was flashed.

## Usage

1. Extract the `.d6` firmware image from Dell's `.EXE` package (the ESM/
   iDRAC firmware download; 7-Zip or a similar archive tool can usually
   pull `payload/firmimg.d6` straight out of it, or run the `.EXE` on
   Windows and grab it from the extraction temp folder).
2. Copy `.env.example` to `.env` and fill in your iDRAC's IP, username, and
   password (`key:value` format, one per line, not `KEY=VALUE`).
3. Run it:
   ```
   python idrac_flash.py .env firmimg.d6
   ```

Example output:

```
=== idrac_flash.py 1.3.0 ===
Image: firmimg.d6 (56.8 MB) -> 192.168.1.100:443
Image SHA-256: 3f2a...c91b
Logging in...
Login OK.
Current firmware (before): 1.98 (Build 06)
Uploading firmware image (this can take a few minutes)...
Uploading... 10%
Uploading... 20%
...
Upload complete in 178s.
Waiting for image to be staged...
Image staged and validated. Target version: 2.92 (Build 05)
Triggering flash commit...
Flash commit accepted. Flashing now; the iDRAC will reboot itself...
fwProgress=30 state=4
fwProgress=70 state=4
iDRAC stopped responding; it's rebooting into the new firmware now.
Waiting for the iDRAC to come back online (this can take a few minutes)...
...still rebooting (30s elapsed)
...still rebooting (60s elapsed)
=== iDRAC is back online. ===
Firmware before update: 1.98 (Build 06)
Firmware after update:  2.92 (Build 05)
Target (staged image):  2.92 (Build 05)
Total run time: 412s
Verification: SUCCESS - running firmware matches the flashed image.
```

The script exits `0` only on a verified success (the post-reboot version
matches the image that was staged). It exits non-zero and says so if the
upload or the flash commit is rejected, if the iDRAC never reboots, or if it
comes back on the wrong version, so you never get a false "done" on a flash
that did not actually take. Full detail (every request/response) also goes to
`idrac_flash_log.txt` next to the script.

Before uploading it prints the SHA-256 of the `.d6` file so you can check it
against Dell's published checksum for the firmware package.

If your iDRAC's web interface is on a non-standard port, add a `port:` line
to the env file (default is 443). The poll intervals and timeouts can be
overridden with `IDRAC_*` environment variables if you ever need to (see the
top of the script).

### Optional: back up the current config first

`idrac_flash.py` always sends `preConfig=on`, which keeps your iDRAC's
configuration through the flash, but that's preservation during the flash,
not a backup you could restore from later. `idrac_backup_config.py` is a
separate, optional script that reads the current config over SSH
(`racadm getconfig -g <group>` for network, users, RAC security tuning,
serial/console, SNMP and IPMI settings) and saves it to a local text file,
so you have your own copy before touching firmware. Run it on its own:

```
python idrac_backup_config.py .env backup.cfg
python idrac_flash.py .env firmimg.d6
```

or let `idrac_flash.py` call it for you with `--backup`:

```
python idrac_flash.py .env firmimg.d6 --backup
```

`--backup` just shells out to `idrac_backup_config.py` as a separate process
before flashing (same envfile, default output filename); it's voluntary and
kept as a subprocess call rather than an import so `idrac_flash.py` itself
still has no third-party dependency, only the backup script does. If the
backup fails (e.g. paramiko missing or SSH login rejected), the flash is
aborted with that script's error message; run without `--backup` to skip the
check entirely.

This is the one script in the repo with a dependency: it talks racadm over
SSH instead of the web interface, which needs `paramiko==2.11.0` specifically
(newer paramiko versions are known to fail against iDRAC6's old SSH
algorithms). Install it with:

```
pip install paramiko==2.11.0
```

It's a best-effort plain-text snapshot for human reference and disaster
recovery, not a restorable Lifecycle Controller export (iDRAC6 predates that
feature). If a config group comes back empty or errors, the script warns you
and names the group, since your firmware revision may use slightly different
group names than the ones this script checks by default.

**Note:** the script confirms the iDRAC card itself came back online with
the new firmware. It does *not* check the host server's power state. An
iDRAC firmware flash only affects the management card, not the server
chassis, so there's nothing for it to check there. If you separately care
whether the server stayed powered off/on through this (e.g. because you're
away and don't want to come home to a server that turned itself on), check
that on your own via `racadm serveraction powerstatus` over SSH; that's a
different auth path (SSH/racadm, not this script's web/HTTPS one) and
outside the scope of this repo.

No third-party dependencies; everything uses Python's standard library
(`ssl`, `socket`, `re`, `hashlib`, `urllib.parse`).

## Testing

The `tests/` folder ships a small mock iDRAC server so the whole flow can be
exercised without real hardware or network access:

```
bash tests/run_test.sh
```

It generates a throwaway self-signed cert, then runs `idrac_flash.py` against
the mock through the happy path plus the failure cases (rejected commit,
post-reboot version mismatch, wrong password, unreachable host) and checks
the exit code of each.

`idrac_backup_config.py` has its own test in the same style, against a mock
SSH/racadm shell instead of a mock web server (needs `paramiko==2.11.0`):

```
bash tests/run_test_backup.sh
```

## The problem

The documented way to update an iDRAC6's firmware is:

```
racadm fwupdate -g -u -a <ip> -d payload
```

This tells the iDRAC to pull the firmware image via TFTP from a server you
run. On old iDRAC6 firmware (this was tested on 1.98, on a Dell PowerEdge
R710 with an 11G iDRAC6 Enterprise card) this **reproducibly fails partway
through the transfer**, aborting after roughly 2.6-5.0 MB out of a ~57 MB
image (`ESM_Firmware_*_A00.EXE` extracted to `firmimg.d6`).

This happens regardless of:
- which TFTP server you use (a custom Python server and Tftpd64 v4.70 both
  showed the identical abort point)
- transfer speed or added artificial delay per block (tested up to 129s
  patience per block, 40 retries, and a 4ms/block throttle — none of it
  helped)
- local firewall rules (an explicit allow rule for the TFTP port made no
  difference)
- VPN/network security software running on the client (stopping all VPN
  services made no difference)
- resetting the iDRAC's network stack (`racadm racreset`)

That ruled out the client machine, the network path, and the TFTP server
implementation. It points to a bug/limitation in the TFTP client baked into
the old iDRAC6 firmware itself for transfers of this size. This is a
long-standing, apparently under-documented issue on very old 11G iDRACs.

No harm was done by any of these failed attempts — each abort was cleanly
discarded by the iDRAC and the firmware version stayed unchanged.

## The workaround that works

The iDRAC6 web UI has its own upload path (`fwupload.esp`) that goes over
plain HTTPS instead of TFTP, and it works fine even for the full-size image.
The catch: the iDRAC6's embedded webserver (Mbedthis-Appweb/2.4.2) only
speaks TLS 1.0 with very old cipher suites. Modern browsers refuse this
outright (`ERR_SSL_VERSION_OR_CIPHER_MISMATCH`), but a raw Python socket
with an explicit low `SECLEVEL` and `TLSv1` minimum version connects to it
without issue.

`idrac_flash.py` scripts the browser's own upload/flash flow end to end:

1. **Login** — `POST /data/login` with `user=...&password=...`, returns a
   session cookie (`_appwebSessionId_`). Success is `<authResult>0</authResult>`.
   (Note: iDRAC6 only allows a small number of concurrent GUI sessions. If
   login is rejected, free up old sessions over SSH first:
   `racadm getssninfo` + `racadm closessn -i <ID>`.)
2. **Upload** — `POST /fwupload/fwupload.esp` as `multipart/form-data`,
   field `firmwareUpdate` = the firmware image, plus `preConfig=on` to keep
   the existing iDRAC configuration. Took about 3 minutes for 56.8 MB in
   testing. Success is `<receivedBytes>...</receivedBytes>` — the full file
   arrives intact, unlike the TFTP path.
3. **Wait for staging** — polls `GET /data?get=fwUpdateState,spfwVer` until
   `fwUpdateState=1` (image validated, waiting).
4. **Trigger the flash commit** — this is the part that isn't obvious from
   just watching network traffic. The web UI's `submitFlash()` JS function
   does exactly this sequence: `GET /data?set=fwUpdateState:4`, then
   `GET /data?set=fwUpdate:1`. The `:1` on `fwUpdate` means "keep the
   existing configuration"; omitting it returns `Bad request format`. The
   script checks both responses and aborts if the iDRAC rejects the commit.
5. **Monitor the flash** — polls `fwProgress` (30 -> 70 -> ...) until the
   iDRAC drops the connection as it writes the new firmware and reboots
   itself (roughly 3-5 minutes). A dropped connection is only treated as a
   reboot once there's actual evidence the flash started, so a transient
   network blip isn't mistaken for success.
6. **Wait for the reboot** — retries logging back in until the iDRAC
   responds again.
7. **Verify** — reads `spfwVer` after the reboot and compares it against the
   version the staged image reported in step 3. It reports SUCCESS only if
   they match; a wrong-version comeback is flagged as a failure.

## Other files

- `idrac_web_explore.py <envfile>` — helper that logs in and lists the
  `data?get=`/`data?set=` endpoints referenced on the update page. Only
  needed if a different iDRAC6 firmware version changes something and
  `idrac_flash.py` needs updating to match.
- `idrac_backup_config.py <envfile> [output.cfg]` — optional pre-flash config
  backup over SSH/racadm. See "Optional: back up the current config first"
  above. The only script here with a third-party dependency (`paramiko`).

## Security considerations

- This script deliberately disables TLS certificate verification
  (`check_hostname=False`, `verify_mode=CERT_NONE`) and forces the
  connection down to TLS 1.0 with `SECLEVEL=0`. That's required because the
  iDRAC6's embedded webserver can't do better — but it also means the
  connection has **no protection against man-in-the-middle attacks**. Only
  run this against an iDRAC on a trusted, isolated management network (e.g.
  its own VLAN), never across the open internet or an untrusted network.
  `idrac_backup_config.py` makes the same trade-off on the SSH side
  (`AutoAddPolicy`, no host key verification) for the same reason.
- Your iDRAC password is read from `.env` and sent in the login request.
  Keep `.env` out of version control — this repo's `.gitignore` already
  excludes it, but double-check before pushing if you copy these scripts
  elsewhere.
- `idrac_flash_log.txt` can contain internal iDRAC state; the script never
  logs your password or session cookie, but treat the log as operational
  data for your own eyes rather than something to attach to a public bug
  report or forum post. The same applies to whatever `idrac_backup_config.py`
  writes out: it's your actual network/user/alerting configuration, treat it
  like the credentials file, not something to paste into a public issue.

## Disclaimer

This talks to old, no-longer-updated embedded firmware over deliberately
weakened TLS settings, and triggers a firmware flash on your management
controller. It worked reliably in testing on an iDRAC6 Enterprise card
(PowerEdge R710) going from 1.98 to 2.92, but there is always some risk in
flashing management firmware remotely. Make sure you have a working
out-of-band way to recover (physical/local access, another admin on site)
before you rely on this against a server you can't walk up to. Use at your
own risk. Not affiliated with or endorsed by Dell.

## License

MIT — see `LICENSE`.
