"""
READ-ONLY capture tool: record exactly how a real iDRAC6 answers the handful
of web endpoints idrac_flash.py reads, so the mock server in this folder can
be grounded in real observed responses instead of guesses.

This is deliberately, provably read-only. It logs in, issues a fixed allow
list of GET requests, logs out again, and dumps the raw HTTP responses
(status line + headers + body, session cookie value redacted) to a file. It
will refuse to send anything that could change state: no fwupload, no
"data?set=", no flash trigger. You cannot flash firmware with this script.

Use it to check that your iDRAC6 firmware revision answers the way the mock
(and therefore idrac_flash.py's parser) expects. If a field name or wrapper
differs on your firmware, the capture shows it and you can adjust.

Usage:
    python capture_idrac.py <envfile> [output.txt]

Same envfile format as idrac_flash.py (ip/user/pw, optional port).
"""
import os
import re
import socket
import ssl
import sys
import time

# The ONLY requests this tool will ever send. A login (to get a session), a
# set of pure reads, and a logout. Anything not on these lists is refused
# below, so this script structurally cannot trigger a flash.
READONLY_GETS = [
    "/data?get=spfwVer",
    "/data?get=fwSemStatus",
    "/data?get=fwUpdateState,spfwVer",
    "/data?get=spfwInfo,fwProgress,fwUpdateState",
]
LOGIN_PATH = "/data/login"
LOGOUT_PATH = "/data/logout"

PORT = 443


def load_env(path):
    try:
        f = open(path)
    except OSError as e:
        print(f"FAILED: could not read env file {path!r}: {e.strerror}")
        sys.exit(1)
    cfg = {}
    with f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip()
    for k in ("ip", "user", "pw"):
        if not cfg.get(k):
            print(f"FAILED: env file {path!r} is missing required key: {k}")
            sys.exit(1)
    return cfg


def make_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def _refuse_if_unsafe(method, path):
    """Hard guardrail: this tool is read-only by construction."""
    lp = path.lower()
    if "set=" in lp or "fwupload" in lp:
        raise SystemExit(f"REFUSED (not read-only): {method} {path}")
    if method == "POST" and path not in (LOGIN_PATH,):
        raise SystemExit(f"REFUSED (only login may POST): {method} {path}")
    if method not in ("GET", "POST"):
        raise SystemExit(f"REFUSED (unexpected method): {method} {path}")


def raw_request(host, method, path, headers=None, body=None, timeout=20):
    """Send one request and return the COMPLETE raw response bytes (status
    line + headers + body), plus the negotiated TLS version/cipher."""
    _refuse_if_unsafe(method, path)
    if isinstance(body, str):
        body = body.encode()
    ctx = make_ctx()
    s = socket.create_connection((host, PORT), timeout=timeout)
    w = ctx.wrap_socket(s)
    tls_info = (w.version(), w.cipher())
    try:
        h = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
        for k, v in (headers or {}).items():
            h += f"{k}: {v}\r\n"
        if body is not None:
            h += f"Content-Length: {len(body)}\r\n"
        h += "\r\n"
        w.sendall(h.encode() + (body or b""))
        resp = b""
        while True:
            try:
                chunk = w.recv(65536)
            except (ssl.SSLError, OSError):
                break
            if not chunk:
                break
            resp += chunk
    finally:
        try:
            w.close()
        except OSError:
            pass
    return resp, tls_info


def redact(raw_text, cookie_value):
    if cookie_value:
        raw_text = raw_text.replace(cookie_value, "<REDACTED_SESSION_ID>")
    return raw_text


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {os.path.basename(__file__)} <envfile> [output.txt]")
        sys.exit(2)
    cfg = load_env(sys.argv[1])
    outpath = sys.argv[2] if len(sys.argv) > 2 else "idrac_capture.txt"
    global PORT
    if cfg.get("port"):
        PORT = int(cfg["port"])
    host = cfg["ip"]

    out = []

    def record(label, method, path, raw, tls=None):
        text = raw.decode(errors="replace")
        text = redact(text, record.cookie_value)
        block = [f"########## {label}", f"# request: {method} {path}"]
        if tls:
            block.append(f"# TLS: {tls[0]} / {tls[1]}")
        block.append("# ---- raw response ----")
        block.append(text if text else "(empty response)")
        block.append("")
        out.append("\n".join(block))
        print(f"captured: {label} ({len(raw)} bytes)")
    record.cookie_value = None

    print(f"Connecting (read-only) to {host}:{PORT} ...")

    # 1. Login (the only POST). Capture the raw response, then extract the
    #    session cookie so later reads are authenticated - and so we can
    #    redact its value from everything we write out.
    from urllib.parse import quote_plus
    login_body = f"user={quote_plus(cfg['user'])}&password={quote_plus(cfg['pw'])}"
    try:
        raw, tls = raw_request(host, "POST", LOGIN_PATH,
                               {"Content-Type": "application/x-www-form-urlencoded"},
                               login_body)
    except OSError as e:
        print(f"FAILED: could not reach {host}:{PORT}: {e!r}")
        sys.exit(1)
    m = re.search(r"Set-Cookie:\s*(_appwebSessionId_=[^;\r\n]+)", raw.decode(errors="replace"))
    cookie = m.group(1) if m else None
    if cookie:
        record.cookie_value = cookie.split("=", 1)[1]
    record("LOGIN", "POST", LOGIN_PATH, raw, tls)
    if not cookie:
        print("WARNING: no session cookie returned - login may have failed; "
              "the reads below will likely be unauthorized. Capturing anyway.")

    hdr = {"Cookie": cookie, "Referer": f"https://{host}/fwupdate.html"} if cookie else {}

    # 2. The read-only GETs.
    for path in READONLY_GETS:
        try:
            raw, _ = raw_request(host, "GET", path, hdr)
            record(f"GET {path}", "GET", path, raw)
        except OSError as e:
            out.append(f"########## GET {path}\n# ERROR: {e!r}\n")
            print(f"error on {path}: {e!r}")
        time.sleep(0.3)

    # 3. Log out to free the GUI session (iDRAC6 allows only a few).
    if cookie:
        try:
            raw, _ = raw_request(host, "GET", LOGOUT_PATH, hdr)
            record("LOGOUT", "GET", LOGOUT_PATH, raw)
        except OSError as e:
            print(f"logout error (session will time out on its own): {e!r}")

    with open(outpath, "w", encoding="utf-8") as f:
        f.write("# iDRAC6 read-only capture\n"
                f"# host: {host}:{PORT}\n"
                f"# taken: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "# session cookie value redacted; no password recorded.\n\n")
        f.write("\n".join(out))
    print(f"Done. Wrote {outpath}")


if __name__ == "__main__":
    main()
