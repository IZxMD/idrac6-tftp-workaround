"""
One-shot iDRAC6 firmware update via the web interface (TLS 1.0), no browser
needed. Handles the whole flow end to end: login, upload, trigger the flash
commit, monitor progress, wait for the iDRAC to come back online after its
self-reboot, and confirm the new version.

Works around the broken TFTP client in old iDRAC6 firmware (seen on 1.98,
possibly other early revisions) that aborts large TFTP transfers after a
few MB. See README.md for the full root-cause analysis and background.

Usage:
    python idrac_flash.py <envfile> <firmware_image>

Example:
    python idrac_flash.py .env firmimg.d6

envfile format (key:value per line, NOT key=value):
    ip:192.168.1.100
    user:root
    pw:your-idrac-password

Prints live progress to the terminal; full detail also goes to
idrac_flash_log.txt next to this script.

Note: this only confirms the iDRAC card itself comes back online with the
new firmware version. It does NOT check the host server's power state —
an iDRAC firmware flash doesn't touch the server chassis, but if you also
want to confirm the server stayed powered off/on as expected, do that
separately (e.g. via `racadm serveraction powerstatus` over SSH).
"""
import os
import re
import ssl
import socket
import sys
import time
from urllib.parse import quote_plus

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = open(os.path.join(HERE, "idrac_flash_log.txt"), "a", buffering=1, encoding="utf-8")

# Tunables
UPLOAD_TIMEOUT = 900        # seconds to allow for the multipart upload itself
STAGE_POLL_INTERVAL = 5     # seconds between "is the image staged yet" checks
STAGE_TIMEOUT = 120         # seconds to wait for fwUpdateState to reach 1
PROGRESS_POLL_INTERVAL = 15 # seconds between fwProgress checks during flashing
PROGRESS_TIMEOUT = 20 * 60  # seconds to wait for the iDRAC to drop off (flashing)
REBOOT_POLL_INTERVAL = 15   # seconds between reconnect attempts after reboot
REBOOT_TIMEOUT = 20 * 60    # seconds to wait for the iDRAC to come back online


def log(msg):
    LOG.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def status(msg):
    """Print to the terminal AND the log file."""
    print(msg, flush=True)
    log(msg)


def load_env(path):
    cfg = {}
    for line in open(path):
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def make_ctx():
    # iDRAC6's embedded webserver (Mbedthis-Appweb/2.4.2) only speaks
    # TLS 1.0 / old cipher suites that modern OpenSSL disables by default,
    # hence the explicit minimum_version + SECLEVEL=0 below. This means
    # the connection has NO certificate validation and NO protection
    # against man-in-the-middle attacks — only use this on a trusted,
    # isolated management network. See README.md "Security considerations".
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def http(host, method, path, headers=None, body=None, timeout=30, chunk=65536, progress_cb=None):
    ctx = make_ctx()
    s = socket.create_connection((host, 443), timeout=timeout)
    w = ctx.wrap_socket(s)
    h = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
    for k, v in (headers or {}).items():
        h += f"{k}: {v}\r\n"
    if body is not None:
        if isinstance(body, str):
            body = body.encode()
        h += f"Content-Length: {len(body)}\r\n"
    h += "\r\n"
    w.send(h.encode())
    if body:
        sent = 0
        while sent < len(body):
            w.send(body[sent:sent + chunk])
            sent += chunk
            if progress_cb:
                progress_cb(sent, len(body))
    resp = b""
    while True:
        try:
            data = w.recv(65536)
        except Exception:
            break
        if not data:
            break
        resp += data
    w.close()
    head, _, b = resp.partition(b"\r\n\r\n")
    return head.decode(errors="replace"), b


def login(host, cfg, timeout=30):
    login_body = f"user={quote_plus(cfg['user'])}&password={quote_plus(cfg['pw'])}"
    head, body = http(host, "POST", "/data/login",
                       {"Content-Type": "application/x-www-form-urlencoded"},
                       login_body, timeout=timeout)
    m = re.search(r"Set-Cookie: (_appwebSessionId_=[^;]+)", head)
    auth = re.search(r"<authResult>(\d+)</authResult>", body.decode(errors="replace"))
    if m and auth and auth.group(1) == "0":
        return m.group(1)
    return None


def ref_headers(host, cookie):
    return {"Cookie": cookie, "Referer": f"https://{host}/fwupdate.html"}


def fail(msg):
    status(f"FAILED: {msg}")
    sys.exit(1)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: python {os.path.basename(__file__)} <envfile> <firmware_image>")
        sys.exit(2)
    envpath, imgpath = sys.argv[1], sys.argv[2]
    cfg = load_env(envpath)
    host = cfg["ip"]
    run_start = time.time()

    img = open(imgpath, "rb").read()
    status(f"=== iDRAC6 flash: {imgpath} ({len(img)/1_000_000:.1f} MB) -> {host} ===")

    # 1. Login
    status("Logging in...")
    try:
        cookie = login(host, cfg)
    except Exception as e:
        fail(f"could not reach {host}: {e!r} — check the ip in your envfile "
             f"and that the iDRAC is on the network")
    if not cookie:
        fail("login rejected — check ip/user/pw in your envfile")
    hdr = ref_headers(host, cookie)
    status("Login OK.")

    # 2. Check semaphore (another update already in progress?)
    try:
        _, body = http(host, "GET", "/data?get=fwSemStatus", hdr)
        log(f"fwSemStatus: {body[:300].decode(errors='replace')}")
    except Exception as e:
        fail(f"lost connection to {host} right after login: {e!r}")

    # 3. Multipart upload, with live progress
    boundary = "----FwUpdateBoundary7391"
    parts = [
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"firmwareUpdate\"; filename=\"firmimg.d6\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + img + b"\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"preConfig\"\r\n\r\non\r\n".encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    payload = b"".join(parts)

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
        head, body = http(host, "POST", "/fwupload/fwupload.esp", upload_hdr, payload,
                           timeout=UPLOAD_TIMEOUT, progress_cb=upload_progress)
    except Exception as e:
        fail(f"upload exception after {time.time()-t0:.0f}s: {e!r}")
    log(f"Upload response: {head.splitlines()[0] if head else '(empty)'} / {body[:500].decode(errors='replace')}")
    if b"receivedBytes" not in body:
        fail(f"upload did not confirm receipt — response: {body[:300].decode(errors='replace')}")
    status(f"Upload complete in {time.time()-t0:.0f}s.")

    # 4. Wait for the image to be validated/staged
    status("Waiting for image to be staged...")
    t0 = time.time()
    staged = False
    while time.time() - t0 < STAGE_TIMEOUT:
        try:
            _, body = http(host, "GET", "/data?get=fwUpdateState,spfwVer", hdr, timeout=20)
        except Exception as e:
            fail(f"lost connection to {host} while waiting for staging: {e!r}")
        txt = body.decode(errors="replace")
        log(f"stage check: {txt.strip()}")
        state = re.search(r"<fwUpdateState>(\d+)</fwUpdateState>", txt)
        if state and state.group(1) == "1":
            staged = True
            break
        time.sleep(STAGE_POLL_INTERVAL)
    if not staged:
        fail("image never reached the staged state (fwUpdateState=1) — check idrac_flash_log.txt")
    status("Image staged and validated.")

    # 5. Trigger the flash commit: exactly what the web UI's submitFlash()
    #    + updateSubmit() do. The ":1" on fwUpdate keeps preConfig.
    status("Triggering flash commit...")
    try:
        _, body = http(host, "GET", "/data?set=fwUpdateState:4", hdr)
        log(f"set=fwUpdateState:4 -> {body[:150].decode(errors='replace').strip()}")
        head, body = http(host, "GET", "/data?set=fwUpdate:1", hdr)
        log(f"set=fwUpdate:1 -> {head.splitlines()[0]} / {body[:200].decode(errors='replace').strip()}")
    except Exception as e:
        fail(f"lost connection to {host} while triggering the flash commit: {e!r} "
             f"— the image is staged, you may be able to retry or finish this from the web UI")

    # 6. Monitor progress until the iDRAC drops off (it reboots itself)
    status("Flashing in progress — this will end with the iDRAC rebooting itself...")
    t0 = time.time()
    last_line = ""
    dropped = False
    while time.time() - t0 < PROGRESS_TIMEOUT:
        time.sleep(PROGRESS_POLL_INTERVAL)
        try:
            _, body = http(host, "GET", "/data?get=spfwInfo,fwProgress,fwUpdateState", hdr, timeout=15)
            txt = body.decode(errors="replace")
            prog = re.search(r"<fwProgress>([^<]*)</fwProgress>", txt)
            line = f"fwProgress={prog.group(1) if prog else '?'}"
            if line != last_line:
                status(line)
                last_line = line
        except Exception:
            status("iDRAC stopped responding — it's rebooting into the new firmware now.")
            dropped = True
            break
    if not dropped:
        fail("flash never seemed to start (iDRAC stayed reachable) — check idrac_flash_log.txt")

    # 7. Wait for the iDRAC to come back online
    status("Waiting for the iDRAC to come back online (this can take a few minutes)...")
    t0 = time.time()
    back_up = False
    new_cookie = None
    while time.time() - t0 < REBOOT_TIMEOUT:
        time.sleep(REBOOT_POLL_INTERVAL)
        try:
            new_cookie = login(host, cfg, timeout=10)
        except Exception:
            new_cookie = None
        if new_cookie:
            back_up = True
            break
        status(f"...still rebooting ({int(time.time()-t0)}s elapsed)")
    if not back_up:
        fail(f"iDRAC did not come back within {REBOOT_TIMEOUT}s — check on it manually")

    # 8. Final version check
    hdr = ref_headers(host, new_cookie)
    try:
        _, body = http(host, "GET", "/data?get=spfwVer", hdr)
        txt = body.decode(errors="replace")
        m = re.search(r"<spfwVer>([^<]*)</spfwVer>", txt)
        version = m.group(1) if m else "(unknown — see idrac_flash_log.txt)"
    except Exception as e:
        status(f"iDRAC is back online but the final version check failed ({e!r}) — "
               f"verify manually, e.g. via the web UI or racadm getversion.")
        return

    status("=== iDRAC is back online. ===")
    status(f"Reported iDRAC firmware version: {version}")
    status(f"Total run time: {int(time.time() - run_start)}s")
    status("Done. This only confirms the iDRAC card's own firmware/state — "
            "if you also track the host server's power status separately, check that now too.")


if __name__ == "__main__":
    main()
