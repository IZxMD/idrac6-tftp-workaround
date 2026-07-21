"""
One-shot iDRAC6 firmware update via the web interface (TLS 1.0), no browser
needed. Handles the whole flow end to end: login, upload, trigger the flash
commit, monitor progress, wait for the iDRAC to come back online after its
self-reboot, and verify the firmware version actually changed to the one
that was staged.

Works around the broken TFTP client in old iDRAC6 firmware (seen on 1.98,
possibly other early revisions) that aborts large TFTP transfers after a
few MB. See README.md for the full root-cause analysis and background.

Usage:
    python idrac_flash.py <envfile> <firmware_image> [--backup] [--force]
    python idrac_flash.py <envfile> --check
    python idrac_flash.py --version

Example:
    python idrac_flash.py .env firmimg.d6

--check: read-only preflight. Logs in, reads the session token, running
firmware version and update semaphore, prints them, and exits WITHOUT
uploading or flashing anything. Safe to run against a live iDRAC any time,
e.g. to confirm credentials and see the current version before a flash.

Firmware dialects: this speaks both the old 1.9x web API (cookie-only auth,
version in spfwVer) and the newer 2.x one (ST2 session-token header, version
in fwVersion). It detects and uses whichever the iDRAC presents.

--backup: optionally runs idrac_backup_config.py first, as a subprocess
(same envfile, default output path). Voluntary and separate on purpose: this
script imports nothing beyond it; --backup just shells out to the other
script, so idrac_backup_config.py's paramiko dependency never touches this
script's own imports. If that script isn't runnable (e.g. paramiko missing),
the flash is aborted with its error message; run without --backup to skip
the check and just flash.

--force: proceed even if the iDRAC's update semaphore is non-zero (another
update may be in progress, or a previous one did not release cleanly). By
default a non-zero semaphore aborts; --force downgrades it to a warning. A
stale lock after an aborted TFTP attempt is the usual reason you'd need it;
racadm racreset also clears it.

envfile format (key:value per line, NOT key=value):
    ip:192.168.1.100
    user:root
    pw:your-idrac-password
    port:443        # optional, defaults to 443

Prints live progress to the terminal; full detail also goes to
idrac_flash_log.txt next to this script.

Note: this only confirms the iDRAC card itself comes back online with the
new firmware version. It does NOT check the host server's power state.
An iDRAC firmware flash doesn't touch the server chassis, but if you also
want to confirm the server stayed powered off/on as expected, do that
separately (e.g. via `racadm serveraction powerstatus` over SSH).
"""
import hashlib
import os
import re
import ssl
import socket
import subprocess
import sys
import time
import warnings
from urllib.parse import quote_plus

# Talking to iDRAC6 over TLS 1.0 is intentional and unavoidable (see make_ctx);
# silence the expected deprecation notice so it doesn't clutter every run.
warnings.filterwarnings("ignore", message=r".*TLSv1.*", category=DeprecationWarning)

__version__ = "1.5.1"


def _restrict(path):
    """Best-effort chmod 0600. The log/backup can contain internal iDRAC
    state and config, so keep them owner-only on POSIX. No-op / harmless on
    Windows, which doesn't honor these bits."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(HERE, "idrac_flash_log.txt")
LOG = open(_LOG_PATH, "a", buffering=1, encoding="utf-8")
_restrict(_LOG_PATH)

# Port of the iDRAC web interface. Overridable via a "port" key in the env
# file (default 443); mainly useful for testing against a local mock.
PORT = 443

def _envint(name, default):
    """Allow overriding a timing constant via environment (handy for tests
    and for tuning against a slow iDRAC); falls back to the default."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# Tunables (each overridable via the matching IDRAC_* environment variable)
UPLOAD_TIMEOUT = _envint("IDRAC_UPLOAD_TIMEOUT", 900)          # upload socket timeout
STAGE_POLL_INTERVAL = _envint("IDRAC_STAGE_POLL", 5)          # between staging checks
STAGE_TIMEOUT = _envint("IDRAC_STAGE_TIMEOUT", 120)          # wait for fwUpdateState=1
PROGRESS_POLL_INTERVAL = _envint("IDRAC_PROGRESS_POLL", 15)  # between fwProgress checks
PROGRESS_TIMEOUT = _envint("IDRAC_PROGRESS_TIMEOUT", 20 * 60)  # wait for the drop-off
REBOOT_POLL_INTERVAL = _envint("IDRAC_REBOOT_POLL", 15)      # between reconnect attempts
REBOOT_TIMEOUT = _envint("IDRAC_REBOOT_TIMEOUT", 20 * 60)     # wait for it to come back


def log(msg):
    LOG.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def status(msg):
    """Print to the terminal AND the log file."""
    print(msg, flush=True)
    log(msg)


def fail(msg):
    status(f"FAILED: {msg}")
    sys.exit(1)


def load_env(path):
    try:
        f = open(path)
    except OSError as e:
        fail(f"could not read env file {path!r}: {e.strerror}")
    cfg = {}
    with f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                cfg[k.strip()] = v.strip()
    missing = [k for k in ("ip", "user", "pw") if not cfg.get(k)]
    if missing:
        fail(f"env file {path!r} is missing required key(s): {', '.join(missing)} "
             f"(format is key:value per line, e.g. 'ip:192.168.1.100')")
    return cfg


def make_ctx():
    # iDRAC6's embedded webserver (Mbedthis-Appweb/2.4.2) only speaks
    # TLS 1.0 / old cipher suites that modern OpenSSL disables by default,
    # hence the explicit minimum_version + SECLEVEL=0 below. This means
    # the connection has NO certificate validation and NO protection
    # against man-in-the-middle attacks. Only use this on a trusted,
    # isolated management network. See README.md "Security considerations".
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def _send_all(w, data, chunk, progress_cb=None, base=0, total=0):
    """Send every byte of `data`, honoring the partial-write contract of
    socket.send() (it may send fewer bytes than requested). Returns the
    number of bytes sent (== len(data)) or raises on a closed socket."""
    sent = 0
    n = len(data)
    while sent < n:
        written = w.send(data[sent:sent + chunk])
        if written == 0:
            raise ConnectionError("socket closed while sending request body")
        sent += written
        if progress_cb and total:
            progress_cb(base + sent, total)
    return sent


def http(host, method, path, headers=None, body=None, timeout=30, chunk=65536, progress_cb=None):
    """Minimal HTTPS client for the iDRAC. `body` may be None, str, bytes, or
    a list of bytes chunks (the list form avoids concatenating the whole
    multipart upload into one buffer). Returns (status_code, head, body)."""
    if isinstance(body, str):
        body = body.encode()
    if isinstance(body, (bytes, bytearray)):
        body = [bytes(body)]
    body_len = sum(len(p) for p in body) if body else 0

    ctx = make_ctx()
    s = socket.create_connection((host, PORT), timeout=timeout)
    w = ctx.wrap_socket(s)
    try:
        h = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
        for k, v in (headers or {}).items():
            h += f"{k}: {v}\r\n"
        if body is not None:
            h += f"Content-Length: {body_len}\r\n"
        h += "\r\n"
        _send_all(w, h.encode(), chunk)
        sent_base = 0
        for part in (body or []):
            _send_all(w, part, chunk, progress_cb=progress_cb, base=sent_base, total=body_len)
            sent_base += len(part)

        resp = b""
        while True:
            try:
                data = w.recv(65536)
            except (socket.timeout, TimeoutError):
                # A read timeout mid-response is not a clean end; surface it.
                raise
            except ConnectionResetError:
                # A reset is a real failure, not an end-of-response; surface it
                # so e.g. a half-received flash-commit reply isn't parsed as OK.
                raise
            except (ssl.SSLError, OSError):
                # iDRAC6's ancient TLS 1.0 stack routinely closes without a
                # clean close_notify (unexpected-EOF SSLError) instead of a
                # zero-length read. On this hardware that IS the normal end of
                # a "Connection: close" response, so treat it as EOF.
                # ponytail: reset is re-raised above; everything else = EOF.
                break
            if not data:
                break
            resp += data
    finally:
        try:
            w.close()
        except OSError:
            pass

    head, _, b = resp.partition(b"\r\n\r\n")
    head_txt = head.decode(errors="replace")
    m = re.match(r"HTTP/\d\.\d\s+(\d{3})", head_txt)
    code = int(m.group(1)) if m else None
    return code, head_txt, b


def login(host, cfg, timeout=30):
    """Log in. Returns (cookie, st1, st2) on success, (None, None, None) on
    failure. Both tokens come from the login response's forwardUrl
    (index.html?ST1=..,ST2=..). On iDRAC 2.x, st2 is the header token for
    /data requests, and st1 is the query-string token the firmware-upload
    POST needs (the real web UI builds the upload action as
    /fwupload/fwupload.esp?ST1=<st1>; captured from a live 2.92 iDRAC). Older
    1.9x firmware issues neither and accepts the cookie alone, in which case
    both are None and simply not sent."""
    login_body = f"user={quote_plus(cfg['user'])}&password={quote_plus(cfg['pw'])}"
    code, head, body = http(host, "POST", "/data/login",
                            {"Content-Type": "application/x-www-form-urlencoded"},
                            login_body, timeout=timeout)
    btxt = body.decode(errors="replace")
    m = re.search(r"Set-Cookie: (_appwebSessionId_=[^;]+)", head)
    auth = re.search(r"<authResult>(\d+)</authResult>", btxt)
    if m and auth and auth.group(1) == "0":
        st1 = re.search(r"ST1=([0-9a-fA-F]+)", btxt)
        st2 = re.search(r"ST2=([0-9a-fA-F]+)", btxt)
        return m.group(1), (st1.group(1) if st1 else None), (st2.group(1) if st2 else None)
    return None, None, None


def ref_headers(host, cookie, st2=None):
    h = {"Cookie": cookie, "Referer": f"https://{host}/fwupdate.html"}
    if st2:
        h["ST2"] = st2
    return h


def get_running_version(host, hdr):
    """Currently-running iDRAC firmware version. 2.x reports it as fwVersion;
    older 1.9x used spfwVer. Ask for both and prefer whichever is populated,
    so this works across firmware revisions. Returns the string, or None."""
    try:
        _, _, body = http(host, "GET", "/data?get=fwVersion,spfwVer", hdr, timeout=15)
    except OSError:
        return None
    txt = body.decode(errors="replace")
    for field in ("fwVersion", "spfwVer"):
        m = re.search(rf"<{field}>([^<]*)</{field}>", txt)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    if "--version" in sys.argv[1:]:
        print(f"idrac_flash.py {__version__}")
        sys.exit(0)

    do_backup = "--backup" in sys.argv[1:]
    force = "--force" in sys.argv[1:]
    do_check = "--check" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a not in ("--backup", "--force", "--check")]

    global PORT
    # --check is a read-only preflight and needs no image; a real flash needs one.
    if do_check:
        if len(args) != 1:
            print(f"Usage: python {os.path.basename(__file__)} <envfile> --check")
            sys.exit(2)
        envpath, imgpath = args[0], None
    else:
        if len(args) != 2:
            print(f"Usage: python {os.path.basename(__file__)} <envfile> <firmware_image> [--backup] [--force]")
            print(f"       python {os.path.basename(__file__)} <envfile> --check")
            print(f"       python {os.path.basename(__file__)} --version")
            sys.exit(2)
        envpath, imgpath = args[0], args[1]

    if do_backup and not do_check:
        status("Backing up current config first (idrac_backup_config.py)...")
        backup_script = os.path.join(HERE, "idrac_backup_config.py")
        result = subprocess.run([sys.executable, backup_script, envpath])
        if result.returncode != 0:
            fail("config backup failed, aborting flash (run without --backup to skip it)")
        status("Config backup done, proceeding with flash.")

    cfg = load_env(envpath)
    host = cfg["ip"]
    if cfg.get("port"):
        try:
            PORT = int(cfg["port"])
        except ValueError:
            fail(f"invalid port in env file: {cfg['port']!r}")
    run_start = time.time()

    if do_check:
        status(f"=== idrac_flash.py {__version__} --check (read-only, no flash) ===")
        status(f"Target: {host}:{PORT}")
    else:
        if not os.path.isfile(imgpath):
            fail(f"firmware image not found: {imgpath}")
        img = open(imgpath, "rb").read()
        digest = sha256_file(imgpath)
        status(f"=== idrac_flash.py {__version__} ===")
        status(f"Image: {imgpath} ({len(img)/1_000_000:.1f} MB) -> {host}:{PORT}")
        status(f"Image SHA-256: {digest}")

    # 1. Login
    status("Logging in...")
    try:
        cookie, st1, st2 = login(host, cfg)
    except OSError as e:
        fail(f"could not reach {host}:{PORT}: {e!r} (check the ip/port in your "
             f"envfile and that the iDRAC is on the network)")
    if not cookie:
        fail("login rejected (check ip/user/pw in your envfile; iDRAC6 also "
             "limits concurrent sessions, see the README if it keeps failing)")
    hdr = ref_headers(host, cookie, st2)
    status(f"Login OK ({'ST2 session token' if st2 else 'cookie-only auth'}).")

    # Record the currently-running version for a real before/after comparison.
    version_before = get_running_version(host, hdr)
    status(f"Current firmware (before): {version_before or 'unknown'}")

    # 2. Read the update semaphore. On firmware that reports it (older 1.9x),
    #    a non-zero value means another update may be underway; abort unless
    #    --force. Newer firmware doesn't expose the field, so this is skipped.
    try:
        _, _, body = http(host, "GET", "/data?get=fwSemStatus", hdr)
    except OSError as e:
        fail(f"lost connection to {host} right after login: {e!r}")
    sem_txt = body.decode(errors="replace")
    log(f"fwSemStatus: {sem_txt.strip()}")
    sem = re.search(r"<fwSemStatus>(\d+)</fwSemStatus>", sem_txt)
    sem_busy = bool(sem and sem.group(1) != "0")

    if do_check:
        status(f"Update semaphore: {sem.group(1) if sem else 'not reported by this firmware'}")
        if version_before:
            status("Read-only check complete: login, session token and version read all OK. "
                   "No upload or flash was performed.")
            sys.exit(0)
        fail("logged in, but could not read the firmware version. The data endpoint "
             "may need a different field on this firmware; check idrac_flash_log.txt.")

    if sem_busy:
        msg = (f"the iDRAC reports an update semaphore of {sem.group(1)} (not 0). "
               f"Another firmware update may be in progress, or a previous one did "
               f"not release cleanly. If nothing is actually running, reset the "
               f"iDRAC (racadm racreset) and retry, or pass --force to flash anyway.")
        if force:
            status(f"WARNING: {msg}\nProceeding because --force was given.")
        else:
            fail(f"aborting: {msg}")

    # 3. Multipart upload, with live progress. Build the body as a list of
    #    chunks so the ~57 MB image is not duplicated into one giant buffer.
    # Boundary format matters here, it is not cosmetic. A real 2.92 iDRAC was
    # captured accepting a 55 MB upload with a long Gecko-style boundary
    # (27 dashes + digits) and returning 200, while our old short boundary
    # ("----FwUpdateBoundary...") made the same 55 MB body fail with HTTP 500
    # at ~6 MB. The iDRAC's Appweb only streams the upload to disk (no 6 MB
    # request-body limit) when the boundary looks like a real browser's; a
    # short one falls back to the 6 MB-capped buffered handler. So mimic the
    # browser: long dash run + a unique numeric suffix.
    boundary = "-" * 27 + str(int(time.time() * 1000))
    prefix = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"firmwareUpdate\"; filename=\"firmimg.d6\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n").encode()
    # preConfig is the "preserve configuration" checkbox; the real form submits
    # it present-but-empty (value=""), which the iDRAC reads as checked. Match
    # that exactly rather than sending "on".
    suffix = (
        b"\r\n"
        + f"--{boundary}\r\nContent-Disposition: form-data; name=\"preConfig\"\r\n\r\n\r\n".encode()
        + f"--{boundary}--\r\n".encode())
    body_parts = [prefix, img, suffix]

    # The real 2.x web UI submits this as a plain multipart form POST, which
    # carries the session token as the ?ST1=<st1> query param, NOT as the ST2
    # header /data uses (a form POST can't set custom headers). Posting to the
    # bare path with only the ST2 header gets a 302 -> start.html and the flash
    # never happens (captured against live 2.92 hardware). So append ?ST1= when
    # we have it, and drop the ST2 header for the upload. Old 1.9x firmware has
    # no st1; there the bare cookie-only path is the correct behavior.
    upload_hdr = dict(hdr)
    upload_hdr.pop("ST2", None)
    upload_hdr["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    upload_path = "/fwupload/fwupload.esp"
    if st1:
        upload_path += f"?ST1={st1}"

    last_pct = [-1]

    def upload_progress(sent, total):
        pct = int(sent * 100 / total)
        if pct != last_pct[0] and pct % 10 == 0:
            status(f"Uploading... {pct}%")
            last_pct[0] = pct

    status("Uploading firmware image (this can take a few minutes)...")
    t0 = time.time()
    try:
        code, head, body = http(host, "POST", upload_path, upload_hdr,
                                body_parts, timeout=UPLOAD_TIMEOUT, progress_cb=upload_progress)
    except OSError as e:
        fail(f"upload failed after {time.time()-t0:.0f}s: {e!r}")
    log(f"Upload response: HTTP {code} / {body[:500].decode(errors='replace')}")
    if code != 200:
        fail(f"upload was not confirmed (HTTP {code}) - response: "
             f"{body[:300].decode(errors='replace')}")
    # The whole reason this tool exists is that TFTP transfers arrived
    # truncated, so don't just check that "receivedBytes" appears, check the
    # value. Use >= rather than == because it's not verified whether the iDRAC
    # counts the image bytes or the whole multipart payload (a few hundred
    # bytes of boundary/headers larger); either way a genuine truncation is
    # far below len(img) and is what we must catch.
    rb = re.search(rb"<receivedBytes>(\d+)</receivedBytes>", body)
    if not rb:
        fail(f"upload response did not report receivedBytes (HTTP {code}) - response: "
             f"{body[:300].decode(errors='replace')}")
    received = int(rb.group(1))
    if received < len(img):
        fail(f"upload incomplete: iDRAC received {received} bytes, expected at least "
             f"{len(img)} (the {len(img)/1_000_000:.1f} MB image). Firmware NOT flashed.")
    status(f"Upload complete in {time.time()-t0:.0f}s ({received} bytes received).")

    # 4. Wait for the image to be validated/staged, and read the version the
    #    staged image reports. That version is our target for verification.
    status("Waiting for image to be staged...")
    t0 = time.time()
    staged = False
    version_target = None
    while time.time() - t0 < STAGE_TIMEOUT:
        try:
            _, _, body = http(host, "GET", "/data?get=fwUpdateState,spfwVer", hdr, timeout=20)
        except OSError as e:
            fail(f"lost connection to {host} while waiting for staging: {e!r}")
        txt = body.decode(errors="replace")
        log(f"stage check: {txt.strip()}")
        state = re.search(r"<fwUpdateState>(\d+)</fwUpdateState>", txt)
        if state and state.group(1) == "1":
            # spfwVer is the staged/target version field on both dialects (unlike
            # fwVersion, which always reports the still-running, pre-flash
            # version and so is no use as a target here). If this firmware
            # doesn't populate it while staged, version_target stays None and
            # the final check correctly reports UNVERIFIED rather than guessing.
            tv = re.search(r"<spfwVer>([^<]*)</spfwVer>", txt)
            version_target = tv.group(1).strip() if tv and tv.group(1).strip() else None
            staged = True
            break
        time.sleep(STAGE_POLL_INTERVAL)
    if not staged:
        fail("image never reached the staged state (fwUpdateState=1) - see idrac_flash_log.txt")
    status(f"Image staged and validated. Target version: {version_target or 'unknown'}")

    # 5. Trigger the flash commit and VALIDATE that the iDRAC accepted it.
    #    Web UI submitFlash(): set fwUpdateState:4 then fwUpdate:1. The ":1"
    #    keeps preConfig; omitting it returns "Bad request format".
    status("Triggering flash commit...")
    try:
        c1, _, b1 = http(host, "GET", "/data?set=fwUpdateState:4", hdr)
        log(f"set=fwUpdateState:4 -> HTTP {c1} / {b1[:150].decode(errors='replace').strip()}")
        c2, _, b2 = http(host, "GET", "/data?set=fwUpdate:1", hdr)
        log(f"set=fwUpdate:1 -> HTTP {c2} / {b2[:200].decode(errors='replace').strip()}")
    except OSError as e:
        fail(f"lost connection to {host} while triggering the flash commit: {e!r} "
             f"(the image is staged; you may be able to finish from the web UI)")
    b2_txt = b2.decode(errors="replace")
    if c1 != 200 or c2 != 200 or "bad request" in b2_txt.lower() or "error" in b2_txt.lower():
        fail(f"iDRAC rejected the flash commit (state4 HTTP {c1}, fwUpdate HTTP {c2}, "
             f"body {b2_txt[:200].strip()!r}). Firmware NOT flashed - see idrac_flash_log.txt")
    status("Flash commit accepted. Flashing now; the iDRAC will reboot itself...")

    # 6. Monitor progress until the iDRAC drops off. A dropped connection is
    #    only accepted as a reboot once we have evidence the flash is actually
    #    running (a well-formed progress/state response), or after repeated
    #    failures, so a single transient blip is not misread as success.
    t0 = time.time()
    last_line = ""
    saw_flash_signal = False
    consecutive_fail = 0
    dropped = False
    while time.time() - t0 < PROGRESS_TIMEOUT:
        time.sleep(PROGRESS_POLL_INTERVAL)
        try:
            _, _, body = http(host, "GET", "/data?get=spfwInfo,fwProgress,fwUpdateState",
                              hdr, timeout=15)
            txt = body.decode(errors="replace")
            consecutive_fail = 0
            prog = re.search(r"<fwProgress>([^<]*)</fwProgress>", txt)
            st = re.search(r"<fwUpdateState>(\d+)</fwUpdateState>", txt)
            if (prog and prog.group(1).strip()) or (st and st.group(1) in ("2", "3", "4")):
                saw_flash_signal = True
            line = f"fwProgress={prog.group(1) if prog else '?'} state={st.group(1) if st else '?'}"
            if line != last_line:
                status(line)
                last_line = line
        except OSError:
            consecutive_fail += 1
            if saw_flash_signal or consecutive_fail >= 2:
                status("iDRAC stopped responding; it's rebooting into the new firmware now.")
                dropped = True
                break
            status("...brief hiccup talking to the iDRAC, retrying...")
    if not dropped:
        fail("the iDRAC never went offline to reboot - the flash may not have started. "
             "Check idrac_flash_log.txt and the iDRAC web UI before retrying.")

    # 7 + 8. Wait for the iDRAC to come back on the NEW firmware, and verify.
    #    Do NOT trust the first successful reconnect: on real hardware the web
    #    interface can briefly drop and return still on the OLD version during
    #    the flash write, a minute or so before the actual firmware-switch
    #    reboot. So poll until the RUNNING VERSION reflects the new firmware,
    #    not just until a login succeeds. This is also the verification:
    #    - if the firmware reported a staged target version, wait for an exact
    #      match;
    #    - if it didn't (some 2.x firmware leaves spfwVer empty while staged),
    #      success is the running version changing from what it was before the
    #      flash. A flash that leaves the version unchanged is a real failure
    #      (or the premature-reconnect blip above), so keep waiting either way.
    status("Waiting for the iDRAC to come back online on the new firmware "
           "(this can take a few minutes)...")
    t0 = time.time()
    version_after = None
    verified = False
    while time.time() - t0 < REBOOT_TIMEOUT:
        time.sleep(REBOOT_POLL_INTERVAL)
        try:
            new_cookie, _new_st1, new_st2 = login(host, cfg, timeout=10)
        except OSError:
            new_cookie = None
        if not new_cookie:
            status(f"...still rebooting ({int(time.time()-t0)}s elapsed)")
            continue
        v = get_running_version(host, ref_headers(host, new_cookie, new_st2))
        if v:
            version_after = v
        if version_target:
            if v and v == version_target:
                verified = True
                break
        elif v and version_before and v != version_before:
            verified = True
            break
        # Reachable but still the old version (or unreadable) means the real
        # firmware-switch reboot hasn't happened yet; keep waiting.
        status(f"...up but not on the new firmware yet "
               f"(reads {v or 'unknown'}, {int(time.time()-t0)}s elapsed)")

    status("=== iDRAC is back online. ===")
    status(f"Firmware before update: {version_before or 'unknown'}")
    status(f"Firmware after update:  {version_after or 'unknown'}")
    status(f"Target (staged image):  {version_target or 'unknown'}")
    status(f"Total run time: {int(time.time() - run_start)}s")

    if verified:
        if version_target:
            status("Verification: SUCCESS - running firmware matches the flashed image.")
        else:
            status(f"Verification: SUCCESS - firmware changed from {version_before} to "
                   f"{version_after}. (This firmware doesn't report the staged image "
                   f"version, so the version change is the confirmation.)")
        sys.exit(0)
    if version_target and version_after and version_after != version_target:
        fail(f"Verification: MISMATCH - iDRAC is up but reports {version_after!r}, "
             f"not the target {version_target!r}. The flash may not have taken; "
             f"verify manually (web UI / racadm getversion) before relying on it.")
    if version_after and version_before and version_after == version_before:
        fail(f"Verification: FAILED - after the flash and the full reboot window the "
             f"iDRAC still reports {version_after!r} (unchanged from before). The flash "
             f"did not take; check the web UI / `racadm getversion` before retrying.")
    status("Verification: UNVERIFIED - iDRAC is back online but the version could "
           "not be read to confirm. Check manually via the web UI or "
           "`racadm getversion`.")
    sys.exit(2)


if __name__ == "__main__":
    main()
