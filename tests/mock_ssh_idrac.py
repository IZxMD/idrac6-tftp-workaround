"""
Minimal fake iDRAC6 SSH/racadm shell for testing idrac_backup_config.py
without real hardware. Accepts one password, opens a shell channel, and
echoes canned racadm output for any "racadm getconfig -g <group>" command
it recognizes (plus a "bad" scenario to exercise the warning path).

Usage:
    python mock_ssh_idrac.py <host_key.pem> [port] [scenario]

Scenarios:
    happy   (default) every known group returns plausible config lines,
            cfgUserAdmin correctly requires -i <index> like real iDRAC6
    partial one group (cfgIpmiLan) returns nothing, to test the WARNING path
    slow    one group's output is sent in two chunks with a pause between
            them (longer than the old settle window), then the prompt, to
            check the reader waits for the prompt instead of truncating
"""
import socket
import sys
import threading
import time

import paramiko

USER = "root"
PW = "test-pass-123"

CANNED = {
    "cfgLanNetworking": "cfgNicEnable=1\ncfgNicIpAddress=192.168.1.100\ncfgDNSDomainName=test.local",
    "cfgRacTuning": "cfgRacTuneHttpPort=80\ncfgRacTuneIpBlkEnable=1\ncfgSerialTelnetEnable=0",
    "cfgSerial": "cfgSerialBaudRate=115200\ncfgSerialConsoleEnable=1",
    "cfgOobSnmp": "cfgOobSnmpAgentCommunity=public",
    "cfgIpmiLan": "cfgIpmiLanEnable=1\ncfgIpmiLanPrivLimit=Administrator",
}
# cfgUserAdmin is indexed on real iDRAC6: `-g cfgUserAdmin` alone errors,
# `-g cfgUserAdmin -i <n>` returns that user slot's config. Only slot 1 is
# populated here, like a real single-user box.
USER_SLOTS = {1: "cfgUserAdminIndex=1\ncfgUserAdminUserName=root\ncfgUserAdminPrivilege=0x1ff"}
SCENARIO = "happy"


class Server(paramiko.ServerInterface):
    def __init__(self):
        self.event = threading.Event()

    def check_auth_password(self, username, password):
        if username == USER and password == PW:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_channel_pty_request(self, *args, **kwargs):
        return True


def handle_client(client_sock, host_key):
    transport = paramiko.Transport(client_sock)
    transport.add_server_key(host_key)
    server = Server()
    try:
        transport.start_server(server=server)
    except Exception as e:
        print(f"[mock-ssh] handshake failed: {e!r}")
        return

    chan = transport.accept(20)
    if chan is None:
        print("[mock-ssh] no channel opened")
        return
    server.event.wait(10)
    print("[mock-ssh] shell opened, sending banner")
    chan.send("iDRAC6 (mock) racadm shell\r\n$ ")

    buf = ""
    while True:
        try:
            data = chan.recv(4096)
        except Exception:
            break
        if not data:
            break
        buf += data.decode(errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            cmd = line.strip()
            if not cmd:
                continue
            print(f"[mock-ssh] got command: {cmd!r}")
            reply = handle_command(cmd)
            if SCENARIO == "slow" and "-g cfgRacTuning" in cmd and reply:
                # Send the reply in two halves with a pause longer than the old
                # 2.5s settle window, then the prompt. A reader that ends on a
                # quiet period alone would truncate here; one that waits for the
                # prompt captures the whole thing.
                mid = len(reply) // 2
                chan.send(reply[:mid])
                time.sleep(3.0)
                chan.send(reply[mid:] + "\r\n$ ")
            else:
                chan.send(reply + "\r\n$ ")
    chan.close()
    transport.close()


def handle_command(cmd):
    if "-g cfgUserAdmin" in cmd:
        if "-i " not in cmd:
            return "ERROR: The indexed group specified requires -i <index>."
        idx = int(cmd.rsplit("-i", 1)[1].strip())
        return USER_SLOTS.get(idx, f"cfgUserAdminIndex={idx}\ncfgUserAdminUserName=")
    for group, text in CANNED.items():
        if f"-g {group}" in cmd:
            if SCENARIO == "partial" and group == "cfgIpmiLan":
                return ""
            return text
    return "ERROR: unknown command"


def main():
    if len(sys.argv) < 2:
        print("usage: python mock_ssh_idrac.py <host_key.pem> [port] [scenario]")
        sys.exit(2)
    host_key = paramiko.RSAKey(filename=sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2222
    global SCENARIO
    SCENARIO = sys.argv[3] if len(sys.argv) > 3 else "happy"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(5)
    print(f"[mock-ssh] listening on 127.0.0.1:{port} (scenario={SCENARIO})")
    while True:
        client_sock, _ = sock.accept()
        threading.Thread(target=handle_client, args=(client_sock, host_key), daemon=True).start()


if __name__ == "__main__":
    main()
