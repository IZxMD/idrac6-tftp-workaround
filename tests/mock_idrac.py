"""
Minimal fake iDRAC6 webserver for exercising idrac_flash.py end to end
without touching real hardware. It implements just enough of the
login / upload / staging / commit / progress / reboot / verify flow to
drive the real script and its error handling.

Usage:
    python mock_idrac.py <cert.pem> <key.pem> [port] [scenario]

Scenarios:
    happy     (default) full successful flash, version goes 1.98 -> 2.92
    reject    the flash commit (fwUpdate:1) is rejected -> script must fail
    mismatch  reboots but comes back on the OLD version -> script must fail

This is test scaffolding, not part of the tool itself.
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
REBOOT_DURATION = 3  # seconds the fake iDRAC "disappears" for

state = {
    "cookie": "testsession123",
    "logged_in": False,
    "fw_state": 0,       # 0 idle, 1 staged, 4 flashing
    "committed": False,
    "progress_polls": 0,
    "rebooting": False,
    "reboot_started": None,
    "running_version": OLD_VERSION,   # what the box reports as running
    "staged_version": None,           # version of the uploaded image
}
SCENARIO = "happy"


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


def respond(conn, body, status="200 OK", extra=""):
    if isinstance(body, str):
        body = body.encode()
    conn.send((f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
               f"Connection: close\r\n{extra}\r\n").encode() + body)


def handle(conn):
    try:
        headers, body = recv_request(conn)
    except Exception:
        conn.close()
        return

    line = headers.splitlines()[0] if headers else ""
    parts = line.split()
    method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
    ck = re.search(r"Cookie:\s*([^\r\n]+)", headers)
    authed = bool(ck and ck.group(1).strip() == f"_appwebSessionId_={state['cookie']}"
                  and state["logged_in"])

    if method == "POST" and path == "/data/login":
        m = re.search(r"user=([^&]*)&password=(.*)", body.decode(errors="replace"))
        if m and m.group(1) == USER and m.group(2) == PW:
            state["logged_in"] = True
            respond(conn, "<authResult>0</authResult>",
                    extra=f"Set-Cookie: _appwebSessionId_={state['cookie']}; path=/\r\n")
            print("[mock] login OK")
        else:
            respond(conn, "<authResult>1</authResult>")
            print("[mock] login REJECTED")

    elif method == "GET" and path == "/data?get=spfwVer":
        respond(conn, f"<spfwVer>{state['running_version']}</spfwVer>")

    elif method == "GET" and path == "/data?get=fwSemStatus":
        respond(conn, "<fwSemStatus>0</fwSemStatus>")

    elif method == "POST" and path == "/fwupload/fwupload.esp":
        if not authed:
            respond(conn, "Unauthorized", status="401 Unauthorized")
        elif b'name="firmwareUpdate"' in body and b'name="preConfig"' in body:
            state["fw_state"] = 1
            state["staged_version"] = NEW_VERSION
            respond(conn, f"<receivedBytes>{len(body)}</receivedBytes>")
            print(f"[mock] upload accepted ({len(body)} B), preConfig present")
        else:
            respond(conn, "Bad request format", status="400 Bad Request")
            print("[mock] upload REJECTED (missing field)")

    elif method == "GET" and path == "/data?get=fwUpdateState,spfwVer":
        # While staged, spfwVer reports the STAGED image version (the target).
        v = state["staged_version"] if state["fw_state"] == 1 else state["running_version"]
        respond(conn, f"<fwUpdateState>{state['fw_state']}</fwUpdateState><spfwVer>{v}</spfwVer>")

    elif method == "GET" and path == "/data?set=fwUpdateState:4":
        respond(conn, "<fwUpdateState>4</fwUpdateState>")
        print("[mock] fwUpdateState -> 4")

    elif method == "GET" and path == "/data?set=fwUpdate:1":
        if SCENARIO == "reject":
            respond(conn, "Bad request format", status="400 Bad Request")
            print("[mock] fwUpdate:1 REJECTED (scenario=reject)")
        else:
            state["committed"] = True
            state["fw_state"] = 4
            respond(conn, "<fwUpdate>1</fwUpdate>")
            print("[mock] flash commit accepted")

    elif method == "GET" and path == "/data?get=spfwInfo,fwProgress,fwUpdateState":
        if state["committed"]:
            state["progress_polls"] += 1
            prog = min(100, state["progress_polls"] * 40)
            respond(conn, f"<fwProgress>{prog}</fwProgress><fwUpdateState>4</fwUpdateState>")
            print(f"[mock] fwProgress={prog}")
            if prog >= 100:
                state["rebooting"] = True
                state["reboot_started"] = time.time()
                print("[mock] rebooting (dropping connections)")
        else:
            respond(conn, "<fwProgress>0</fwProgress><fwUpdateState>0</fwUpdateState>")

    else:
        respond(conn, "not found", status="404 Not Found")

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
        # Genuinely refuse connections while "rebooting", like a real box would.
        if state["rebooting"]:
            if time.time() - state["reboot_started"] >= REBOOT_DURATION:
                state["rebooting"] = False
                state["logged_in"] = False
                state["fw_state"] = 0
                state["committed"] = False
                # On success the running version becomes the flashed one; in the
                # 'mismatch' scenario it stays on the old version on purpose.
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
