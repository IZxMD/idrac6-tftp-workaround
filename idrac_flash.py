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
    python idrac_flash.py <envfile> <firmware_image>
    python idrac_flash.py --version

Example:
    python idrac_flash.py .env firmimg.d6

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
import sys
import time
import warnings
from urllib.parse import quote_plus

# Talking to iDRAC6 over TLS 1.0 is intentional and unavoidable (see make_ctx);
# silence the expected deprecation notice so it doesn't clutter every run.
warnings.filterwarnings("ignore", message=r".*TLSv1.*", category=DeprecationWarning)

__version__ = "1.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = open(os.path.join(HERE, "idrac_flash_log.txt"), "a", buffering=1, encoding="utf-8")

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
            except OSError:
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
    login_body = f"user={quote_plus(cfg['user'])}&password={quote_plus(cfg['pw'])}"
    code, head, body = http(host, "POST", "/data/login",
                            {"Content-Type": "application/x-www-form-urlencoded"},
                            login_body, timeout=timeout)
    m = re.search(r"Set-Cookie: (_appwebSessionId_=[^;]+)", head)
    auth = re.search(r"<authResult>(\d+)</authResult>", body.decode(errors="replace"))
    if m and auth and auth.group(1) == "0":
        return m.group(1)
    return None


def ref_headers(host, cookie):
    return {"Cookie": cookie, "Referer": f"https://{host}/fwupdate.html"}


def get_spfwver(host, hdr):
    """Read the spfwVer field. Returns the version string, or None."""
    try:
        _, _, body = http(host, "GET", "/data?get=spfwVer", hdr, timeout=15)
    except OSError:
        return None
    m = re.search(r"<spfwVer>([^<]*)</spfwVer>", body.decode(errors="replace"))
    v = m.group(1).strip() if m else ""
    return v or None


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
    if len(sys.argv) != 3:
        print(f"Usage: python {os.path.basename(__file__)} <envfile> <firmware_image>")
        print(f"       python {os.path.basename(__file__)} --version")
        sys.exit(2)

    global PORT
    envpath, imgpath = sys.argv[1], sys.argv[2]
    cfg = load_env(envpath)
    host = cfg["ip"]
    if cfg.get("port"):
        try:
            PORT = int(cfg["port"])
        except ValueError:
            fail(f"invalid port in env file: {cfg['port']!r}")
    run_start = time.time()

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
        cookie = login(host, cfg)
    except OSError as e:
        fail(f"could not reach {host}:{PORT}: {e!r} (check the ip/port in your "
             f"envfile and that the iDRAC is on the network)")
    if not cookie:
        fail("login rejected (check ip/user/pw in your envfile; iDRAC6 also "
             "limits concurrent sessions, see the README if it keeps failing)")
    hdr = ref_headers(host, cookie)
    status("Login OK.")

    # Record the currently-running version for a real before/after comparison.
    version_before = get_spfwver(host, hdr)
    status(f"Current firmware (before): {version_before or 'unknown'}")

    # 2. Check the update semaphore and actually act on it.
    try:
        _, _, body = http(host, "GET", "/data?get=fwSemStatus", hdr)
    except OSError as e:
        fail(f"lost connection to {host} right after login: {e!r}")
    sem_txt = body.decode(errors="replace")
    log(f"fwSemStatus: {sem_txt.strip()}")
    sem = re.search(r"<fwSemStatus>(\d+)</fwSemStatus>", sem_txt)
    if sem and sem.group(1) != "0":
        status(f"WARNING: the iDRAC reports an update semaphore of {sem.group(1)} "
               f"(not 0). Another firmware update may already be in progress or a "
               f"previous one did not release cleanly. Proceeding, but if the "
               f"upload is rejected, wait a few minutes or reset the iDRAC "
               f"(racadm racreset) and retry.")

    # 3. Multipart upload, with live progress. Build the body as a list of
    #    chunks so the ~57 MB image is not duplicated into one giant buffer.
    boundary = "----FwUpdateBoundary7391"
    prefix = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"firmwareUpdate\"; filename=\"firmimg.d6\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n").encode()
    suffix = (
        b"\r\n"
        + f"--{boundary}\r\nContent-Disposition: form-data; name=\"preConfig\"\r\n\r\non\r\n".encode()
        + f"--{boundary}--\r\n".encode())
    body_parts = [prefix, img, suffix]

    upload_hdr = dict(hdr)
    upload_hdr["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    last_pct = [-1]

    def upload_progress(sent, total):
        pct = int(sent * 100 / total)
        if pct != last_pct[0] and pct % 10 == 0:
            status(f"Uploading... {pct}%")
            last_pct[0] = pct

    status("Uploading firmware image (this can take a few minutes)...")
    t0 = time.time()
    try:
        code, head, body = http(host, "POST", "/fwupload/fwupload.esp", upload_hdr,
                                body_parts, timeout=UPLOAD_TIMEOUT, progress_cb=upload_progress)
    except OSError as e:
        fail(f"upload failed after {time.time()-t0:.0f}s: {e!r}")
    log(f"Upload response: HTTP {code} / {body[:500].decode(errors='replace')}")
    if code != 200 or b"receivedBytes" not in body:
        fail(f"upload was not confirmed (HTTP {code}) - response: "
             f"{body[:300].decode(errors='replace')}")
    status(f"Upload complete in {time.time()-t0:.0f}s.")

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

    # 7. Wait for the iDRAC to come back online
    status("Waiting for the iDRAC to come back online (this can take a few minutes)...")
    t0 = time.time()
    new_cookie = None
    while time.time() - t0 < REBOOT_TIMEOUT:
        time.sleep(REBOOT_POLL_INTERVAL)
        try:
            new_cookie = login(host, cfg, timeout=10)
        except OSError:
            new_cookie = None
        if new_cookie:
            break
        status(f"...still rebooting ({int(time.time()-t0)}s elapsed)")
    if not new_cookie:
        fail(f"iDRAC did not come back within {REBOOT_TIMEOUT}s - check on it manually")

    # 8. Real verification: compare the post-reboot version against the target.
    hdr = ref_headers(host, new_cookie)
    version_after = get_spfwver(host, hdr)

    status("=== iDRAC is back online. ===")
    status(f"Firmware before update: {version_before or 'unknown'}")
    status(f"Firmware after update:  {version_after or 'unknown'}")
    status(f"Target (staged image):  {version_target or 'unknown'}")
    status(f"Total run time: {int(time.time() - run_start)}s")

    if version_after and version_target and version_after == version_target:
        status("Verification: SUCCESS - running firmware matches the flashed image.")
        sys.exit(0)
    elif version_after and version_target and version_after != version_target:
        fail(f"Verification: MISMATCH - iDRAC is up but reports {version_after!r}, "
             f"not the target {version_target!r}. The flash may not have taken; "
             f"verify manually (web UI / racadm getversion) before relying on it.")
    else:
        status("Verification: UNVERIFIED - iDRAC is back online but the version could "
               "not be read to confirm. Check manually via the web UI or "
               "`racadm getversion`.")
        sys.exit(2)


if __name__ == "__main__":
    main()
