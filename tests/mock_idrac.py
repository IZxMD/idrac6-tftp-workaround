"""
Fake iDRAC6 webserver for exercising idrac_flash.py end to end without real
hardware. Grounded in real captured behavior of an iDRAC6 (firmware 2.92):

  - login returns <authResult>0</authResult> plus a forwardUrl carrying an
    ST1/ST2 session token, and a Set-Cookie;
  - authenticated /data requests require the ST2 token as a header (cookie
    alone -> 401), exactly like the real firmware;
  - /data?get=k1,k2 responses are wrapped as <root>...<status>ok</status></root>
    with each known field echoed as <field>value</field>;
  - the running firmware version is reported as fwVersion (2.x); spfwVer is the
    staged-image version, populated only while an image is staged.

Usage:
    python mock_idrac.py <cert.pem> <key.pem> [port] [scenario]

Scenarios:
    happy     (default) full successful flash, version goes 1.98 -> 2.92
    reject    the flash commit (fwUpdate:1) is rejected -> script must fail
    mismatch  reboots but comes back on the OLD version -> script must fail
    truncated upload reports far fewer receivedBytes than sent -> must fail
    semaphore fwSemStatus non-zero -> abort by default, proceed with --force
    noauth    accepts the cookie WITHOUT the ST2 header (models old 1.9x
              firmware) -> used to prove the client still works cookie-only
    notarget  leaves spfwVer empty while staged (like real 2.85/2.92) -> the
              client must verify success by the running version changing
"""
import re
import socket
import ssl
import sys
import threading
import time

HOST = "127.0.0.1"
USER = "root"
PW = "test-pass-123"
OLD_VERSION = "1.98 (Build 01)"
NEW_VERSION = "2.92 (Build 05)"
REBOOT_DURATION = 3
# Fixed mock session token; the real firmware issues a random one per login.
ST2_TOKEN = "1f20f4fb200020bf2e6ec7a02bb87b73"
ST1_TOKEN = "25028a7a69e004d02ce41d1b01c82f47"

state = {
    "cookie": "testsession123",
    "logged_in": False,
    "fw_state": 0,       # 0 idle, 1 staged, 4 flashing
    "committed": False,
    "progress_polls": 0,
    "rebooting": False,
    "reboot_started": None,
    "running_version": OLD_VERSION,
    "staged_version": None,
}
SCENARIO = "happy"

UNAUTH_HTML = ("<HTML><HEAD><TITLE>Document Error: Unauthorized</TITLE></HEAD>\r\n"
               "<BODY><H2>Access Error: 401 -- Unauthorized</H2>\r\n</BODY></HTML>\r\n")


def recv_request(conn):
    data = b""
    conn.settimeout(10)
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    headers = head.decode(errors="replace")
    m = re.search(r"Content-Length:\s*(\d+)", headers, re.I)
    body = rest
    if m:
        need = int(m.group(1))
        while len(body) < need:
            chunk = conn.recv(min(65536, need - len(body)))
            if not chunk:
                break
            body += chunk
    return headers, body


def respond(conn, body, status="200 OK", ctype="text/xml", extra=""):
    if isinstance(body, str):
        body = body.encode()
    conn.send((f"HTTP/1.1 {status}\r\nServer: Mbedthis-Appweb/2.4.2\r\n"
               f"Content-type: {ctype}\r\nContent-length: {len(body)}\r\n"
               f"Connection: close\r\n{extra}\r\n").encode() + body)


def data_get_response(keys):
    """Build a realistic <root>...<status>ok</status></root> reply echoing
    each known requested field. Unknown keys are silently dropped, exactly
    like the real firmware does with a field it doesn't recognize."""
    parts = ["<root>"]
    for k in keys:
        k = k.strip()
        if k == "fwVersion":
            parts.append(f"<fwVersion>{state['running_version']}</fwVersion>\n")
        elif k == "spfwVer":
            # Only populated while an image is staged (the target version).
            # The "notarget" scenario leaves it empty even while staged, like
            # the real 2.85/2.92 firmware does, so the client must verify by the
            # running version changing rather than by matching a staged target.
            v = ("" if SCENARIO == "notarget"
                 else (state["staged_version"] if state["fw_state"] == 1 else ""))
            parts.append(f"<spfwVer>{v}</spfwVer>\n")
        elif k == "fwUpdateState":
            v = str(state["fw_state"]) if state["fw_state"] else ""
            parts.append(f"<fwUpdateState>{v}</fwUpdateState>\n")
        elif k == "fwProgress":
            prog = min(100, state["progress_polls"] * 40) if state["committed"] else 0
            parts.append(f"<fwProgress>{prog}</fwProgress>\n")
        elif k == "fwSemStatus":
            # Real 2.x omits this; the mock reports it so the semaphore-abort
            # feature (aimed at 1.9x firmware) stays testable.
            sem = "1" if SCENARIO == "semaphore" else "0"
            parts.append(f"<fwSemStatus>{sem}</fwSemStatus>\n")
        elif k == "spfwInfo":
            parts.append("<spfwInfo>0</spfwInfo>\n")
    parts.append("<status>ok</status>\n</root>")
    return "".join(parts)


def handle(conn):
    try:
        headers, body = recv_request(conn)
    except Exception:
        conn.close()
        return

    line = headers.splitlines()[0] if headers else ""
    parts = line.split()
    method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
    ck = re.search(r"Cookie:\s*([^\r\n]*_appwebSessionId_=[^;\r\n]+)", headers)
    st2 = re.search(r"(?im)^ST2:\s*([0-9a-fA-F]+)", headers)
    cookie_ok = bool(ck and f"_appwebSessionId_={state['cookie']}" in ck.group(1)
                     and state["logged_in"])
    # Real firmware (2.x) needs the ST2 header too; "noauth" models old 1.9x
    # that accepted the cookie alone.
    st2_ok = SCENARIO == "noauth" or bool(st2 and st2.group(1) == ST2_TOKEN)
    authed = cookie_ok and st2_ok

    if method == "POST" and path == "/data/login":
        m = re.search(r"user=([^&]*)&password=(.*)", body.decode(errors="replace"))
        if m and m.group(1) == USER and m.group(2) == PW:
            state["logged_in"] = True
            # Old 1.9x firmware (noauth) issues no ST2 token; 2.x does.
            fwd = ("index.html" if SCENARIO == "noauth"
                   else f"index.html?ST1={ST1_TOKEN},ST2={ST2_TOKEN}")
            respond(conn, f'<?xml version="1.0" encoding="UTF-8"?> <root> '
                          f'<status>ok</status> <authResult>0</authResult> '
                          f'<forwardUrl>{fwd}</forwardUrl> </root>',
                    extra=f"Set-Cookie: _appwebSessionId_={state['cookie']}; path=/; secure\r\n")
            print("[mock] login OK")
        else:
            respond(conn, "<root> <status>ok</status> <authResult>1</authResult> </root>")
            print("[mock] login REJECTED")

    elif method in ("GET", "POST") and path.startswith("/data?get="):
        if not authed:
            respond(conn, UNAUTH_HTML, status="401 Unauthorized", ctype="text/html")
            print(f"[mock] 401 on {path[:40]} (cookie_ok={cookie_ok} st2_ok={st2_ok})")
            conn.close()
            return
        keys = path.split("get=", 1)[1].split(",")
        # A progress poll advances the flash and eventually triggers the reboot.
        if "fwProgress" in keys and state["committed"]:
            state["progress_polls"] += 1
            if min(100, state["progress_polls"] * 40) >= 100:
                state["rebooting"] = True
                state["reboot_started"] = time.time()
                print("[mock] rebooting (dropping connections)")
        respond(conn, data_get_response(keys))

    elif method == "POST" and path.startswith("/fwupload/fwupload.esp"):
        # Real 2.x carries the token in the query string (?ST1=<st1>), NOT the
        # ST2 header, because this is a plain multipart form POST. Without a
        # valid ST1 the real esp handler 302-redirects to start.html (captured
        # against live 2.92 hardware), which is what made the flash silently
        # fail. So the mock requires cookie + ST1 query here, not the ST2
        # header. noauth models old 1.9x firmware (cookie alone).
        q = re.search(r"ST1=([0-9a-fA-F]+)", path)
        st1_ok = SCENARIO == "noauth" or bool(q and q.group(1) == ST1_TOKEN)
        if not cookie_ok:
            respond(conn, UNAUTH_HTML, status="401 Unauthorized", ctype="text/html")
            print("[mock] 401 on upload (no cookie)")
        elif not st1_ok:
            respond(conn, b"", status="302 Moved Temporarily",
                    extra=f"Location: https://{HOST}/start.html\r\n")
            print(f"[mock] 302 on upload (missing/bad ST1 query; path={path[:60]})")
        elif (m := re.search(r"boundary=(\S+)", headers, re.I)) and len(m.group(1)) < 20:
            # Models real 2.92 Appweb behavior: a short multipart boundary makes
            # the iDRAC buffer the body (6 MB request-body cap) and return 500
            # mid-upload instead of streaming it. A real browser boundary is a
            # long dash run; the client must mimic that. Guards that regression.
            respond(conn, "<HTML><HEAD><TITLE>Document Error: Internal Server Error"
                    "</TITLE></HEAD><BODY><H2>Access Error: 500</H2></BODY></HTML>",
                    status="500 Internal Server Error", ctype="text/html")
            print(f"[mock] 500 on upload (boundary too short: {m.group(1)!r})")
        elif b'name="firmwareUpdate"' in body and b'name="preConfig"' in body:
            if SCENARIO == "truncated":
                respond(conn, "<receivedBytes>1024</receivedBytes>")
                print(f"[mock] upload TRUNCATED (reported 1024 B of {len(body)})")
            else:
                state["fw_state"] = 1
                state["staged_version"] = NEW_VERSION
                respond(conn, f"<receivedBytes>{len(body)}</receivedBytes>")
                print(f"[mock] upload accepted ({len(body)} B)")
        else:
            respond(conn, "Bad request format", status="400 Bad Request")
            print("[mock] upload REJECTED (missing field)")

    elif path == "/data?set=fwUpdateState:4":
        if not authed:
            respond(conn, UNAUTH_HTML, status="401 Unauthorized", ctype="text/html")
        else:
            respond(conn, "<root><fwUpdateState>4</fwUpdateState><status>ok</status></root>")
            print("[mock] fwUpdateState -> 4")

    elif path == "/data?set=fwUpdate:1":
        if not authed:
            respond(conn, UNAUTH_HTML, status="401 Unauthorized", ctype="text/html")
        elif SCENARIO == "reject":
            respond(conn, "Bad request format", status="400 Bad Request")
            print("[mock] fwUpdate:1 REJECTED (scenario=reject)")
        else:
            state["committed"] = True
            state["fw_state"] = 4
            respond(conn, "<root><fwUpdate>1</fwUpdate><status>ok</status></root>")
            print("[mock] flash commit accepted")

    else:
        respond(conn, "not found", status="404 Not Found", ctype="text/html")

    try:
        conn.close()
    except OSError:
        pass


def main():
    if len(sys.argv) < 3:
        print("usage: python mock_idrac.py <cert.pem> <key.pem> [port] [scenario]")
        sys.exit(2)
    cert, key = sys.argv[1], sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 8443
    global SCENARIO
    SCENARIO = sys.argv[4] if len(sys.argv) > 4 else "happy"

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.listen(5)
    print(f"[mock] listening on https://{HOST}:{port} (scenario={SCENARIO})")

    while True:
        raw, _ = sock.accept()
        if state["rebooting"]:
            if time.time() - state["reboot_started"] >= REBOOT_DURATION:
                state["rebooting"] = False
                state["logged_in"] = False
                state["fw_state"] = 0
                state["committed"] = False
                if SCENARIO != "mismatch":
                    state["running_version"] = NEW_VERSION
                print("[mock] back online")
            else:
                raw.close()
                continue
        try:
            conn = ctx.wrap_socket(raw, server_side=True)
        except OSError as e:
            print(f"[mock] TLS handshake failed: {e!r}")
            raw.close()
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
