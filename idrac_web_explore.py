"""
Explore an iDRAC6 web interface: log in and find the firmware-update pages.

Useful if Dell changes something in a future firmware revision and the
data?get=/data?set= endpoints used by idrac_web_fwupdate.py /
idrac_commit_flash.py need to be rediscovered.

Usage: python idrac_web_explore.py <envfile>

envfile format (key:value per line, NOT key=value):
    ip:192.168.1.100
    user:root
    pw:your-idrac-password
"""
import ssl
import socket
import re
import sys
from urllib.parse import quote_plus


def load_env(path=".env"):
    cfg = {}
    for line in open(path):
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def make_ctx():
    # iDRAC6's embedded webserver (Mbedthis-Appweb/2.4.2) only speaks
    # TLS 1.0 / old cipher suites that modern OpenSSL disables by default.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def http(host, method, path, headers=None, body=None):
    ctx = make_ctx()
    s = socket.create_connection((host, 443), timeout=30)
    w = ctx.wrap_socket(s)
    h = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
    for k, v in (headers or {}).items():
        h += f"{k}: {v}\r\n"
    if body is not None:
        if isinstance(body, str):
            body = body.encode()
        h += f"Content-Length: {len(body)}\r\n"
    h += "\r\n"
    w.send(h.encode() + (body or b""))
    resp = b""
    while True:
        try:
            chunk = w.recv(65536)
        except Exception:
            break
        if not chunk:
            break
        resp += chunk
    w.close()
    head, _, bodyresp = resp.partition(b"\r\n\r\n")
    return head.decode(errors="replace"), bodyresp


def main():
    cfg = load_env(sys.argv[1] if len(sys.argv) > 1 else ".env")
    host = cfg["ip"]

    # Login
    login_body = f"user={quote_plus(cfg['user'])}&password={quote_plus(cfg['pw'])}"
    head, body = http(host, "POST", "/data/login",
                       {"Content-Type": "application/x-www-form-urlencoded"},
                       login_body)
    print("== LOGIN HEAD ==")
    print(head[:400])
    print("== LOGIN BODY ==")
    print(body[:800].decode(errors="replace"))

    cookie = ""
    m = re.search(r"Set-Cookie: (_appwebSessionId_=[^;]+)", head)
    if m:
        cookie = m.group(1)
    print(f"cookie={cookie}")

    # Candidate pages for firmware update
    for page in ["/update.html", "/fwupdate.html", "/firmup.html", "/vFlash.html",
                 "/topbar.html", "/globalnav.html"]:
        head, body = http(host, "GET", page, {"Cookie": cookie})
        status = head.split("\r\n")[0]
        text = body.decode(errors="replace")
        hits = re.findall(
            r'(?:action|href|src|url)\s*[=:]\s*["\']([^"\']*(?:fw|update|upload|firm)[^"\']*)["\']',
            text, re.I)
        print(f"\n== {page}: {status}, {len(body)} B, matches: {sorted(set(hits))[:10]}")


if __name__ == "__main__":
    main()
