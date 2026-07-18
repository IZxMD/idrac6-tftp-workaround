"""
Back up an iDRAC6's configuration over SSH before flashing firmware.

This is a separate, optional script from idrac_flash.py. idrac_flash.py talks
to the iDRAC's web interface over HTTPS and has no third-party dependencies;
this script talks to the iDRAC's racadm shell over SSH instead, which needs
paramiko. Run this once before idrac_flash.py as a cheap safety net: firmware
flashes are supposed to preserve your configuration (idrac_flash.py always
passes preConfig=on), but "supposed to" is not a backup, and a local text
copy of the config costs nothing.

Usage:
    python idrac_backup_config.py <envfile> [output.cfg]

Example:
    python idrac_backup_config.py .env backup-2026-07-18.cfg

envfile format (key:value per line, NOT key=value):
    ip:192.168.1.100
    user:root
    pw:your-idrac-password
    ssh_port:22          # optional, defaults to 22

Requires: paramiko==2.11.0 specifically. Newer paramiko versions fail to
negotiate with iDRAC6's old SSH host key / kex algorithms; this was tested
and confirmed on this hardware, not a guess. Install with:

    pip install paramiko==2.11.0

What this captures: the racadm config groups most likely to matter before a
firmware flash (network, users, alerting, serial/console, RAC security
tuning), each read with `racadm getconfig -g <group>` and captured as plain
text over the SSH shell (the same proven approach used elsewhere in this
project, not the `-f <file>` remote-file variant, which would need a way to
retrieve the file off the iDRAC's own storage). cfgUserAdmin is the one
exception: iDRAC6 treats it as an indexed group (one racadm call per local
user slot, `-i 1` through `-i 16`), confirmed against real hardware - a plain
`-g cfgUserAdmin` call fails outright. This is a best-effort text
snapshot for human reference and disaster recovery, not a byte-perfect
Lifecycle Controller export (iDRAC6 predates that feature; iDRAC7/12G and
later have racadm's "Server Configuration Profile" export instead). Skim the
output once after running this: if a group prints an error instead of
config lines, your firmware may use slightly different group names; adjust
the GROUPS list below and re-run.
"""
import os
import sys
import time

__version__ = "1.0.1"

GROUPS = [
    "cfgLanNetworking",   # iDRAC network config: IP, DNS, VLAN
    "cfgUserAdmin",       # local user accounts
    "cfgRacTuning",       # telnet/http/ssh/ip-blocking security settings
    "cfgSerial",          # serial/console redirection
    "cfgOobSnmp",         # SNMP alerting
    "cfgIpmiLan",         # IPMI-over-LAN settings
]

# cfgUserAdmin is an "indexed" racadm group on iDRAC6 - one slot per local
# user account - and plain `racadm getconfig -g cfgUserAdmin` fails with
# "ERROR: The indexed group specified requires -i <index>." (confirmed
# against real hardware, not a guess). iDRAC6 has 16 local user slots.
INDEXED_GROUPS = {
    "cfgUserAdmin": range(1, 17),
}


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
    missing = [k for k in ("ip", "user", "pw") if not cfg.get(k)]
    if missing:
        print(f"FAILED: env file {path!r} is missing required key(s): {', '.join(missing)}")
        sys.exit(1)
    return cfg


def run_command(chan, cmd, settle=2.5, timeout=15):
    """Send one racadm command over an already-open shell channel and
    collect its output. Old iDRAC6 shells don't send a clean EOF marker,
    so this waits for a quiet period rather than a prompt string."""
    chan.send(cmd + "\n")
    out = ""
    start = time.time()
    last_data = time.time()
    while time.time() - start < timeout:
        if chan.recv_ready():
            out += chan.recv(65536).decode(errors="replace")
            last_data = time.time()
        elif time.time() - last_data > settle:
            break
        else:
            time.sleep(0.2)
    return out


def main():
    if "--version" in sys.argv[1:]:
        print(f"idrac_backup_config.py {__version__}")
        sys.exit(0)
    if len(sys.argv) < 2:
        print(f"Usage: python {os.path.basename(__file__)} <envfile> [output.cfg]")
        print(f"       python {os.path.basename(__file__)} --version")
        sys.exit(2)
    envpath = sys.argv[1]
    outpath = sys.argv[2] if len(sys.argv) > 2 else "idrac_config_backup.cfg"
    cfg = load_env(envpath)

    try:
        import paramiko
    except ImportError:
        print("FAILED: paramiko is required for this script (idrac_flash.py itself has no "
              "dependencies; this backup script is the exception). Install it with:\n"
              "    pip install paramiko==2.11.0\n"
              "(newer paramiko versions are known to fail against iDRAC6's old SSH algorithms)")
        sys.exit(1)

    port = 22
    if cfg.get("ssh_port"):
        try:
            port = int(cfg["ssh_port"])
        except ValueError:
            print(f"FAILED: invalid ssh_port in env file: {cfg['ssh_port']!r}")
            sys.exit(1)

    print(f"Connecting to {cfg['ip']}:{port} over SSH...")
    client = paramiko.SSHClient()
    # iDRAC6 has no way to present a certificate you could actually verify
    # here; this is the SSH-side equivalent of the TLS trade-off idrac_flash.py
    # documents for the web path. See README "Security considerations".
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            cfg["ip"], port=port, username=cfg["user"], password=cfg["pw"],
            look_for_keys=False, allow_agent=False, timeout=15,
        )
    except paramiko.AuthenticationException:
        print("FAILED: SSH login rejected — check ip/user/pw in your envfile.")
        sys.exit(1)
    except OSError as e:
        print(f"FAILED: could not reach {cfg['ip']} over SSH: {e!r}")
        sys.exit(1)

    try:
        chan = client.invoke_shell()
        time.sleep(2)
        if chan.recv_ready():
            chan.recv(65536)  # discard the login banner/prompt

        sections = []
        empty_groups = []
        for group in GROUPS:
            print(f"Reading {group}...")
            indices = INDEXED_GROUPS.get(group)
            if indices:
                # Indexed group: one racadm call per user slot. Most slots
                # are unused and come back with empty field values, which is
                # normal - only an "ERROR:" line means something's wrong.
                slot_bodies = []
                has_error = False
                for i in indices:
                    out = run_command(chan, f"racadm getconfig -g {group} -i {i}")
                    lines = [ln for ln in out.strip().splitlines() if ln.strip() not in ("", "$", ">")]
                    if any(ln.strip().startswith("ERROR:") for ln in lines):
                        has_error = True
                    slot_bodies.append(f"--- index {i} ---\n" + ("\n".join(lines) if lines else "(no output)"))
                if has_error:
                    empty_groups.append(group)
                body = "\n".join(slot_bodies)
            else:
                out = run_command(chan, f"racadm getconfig -g {group}")
                # Strip trailing shell-prompt noise (e.g. a lone "$" or ">" the
                # iDRAC shell echoes back) so a truly empty response is detected
                # as empty instead of "one line of prompt junk".
                content_lines = [ln for ln in out.strip().splitlines() if ln.strip() not in ("", "$", ">")]
                if not content_lines or any(ln.strip().startswith("ERROR:") for ln in content_lines):
                    empty_groups.append(group)
                body = "\n".join(content_lines) if content_lines else "(no output)"
            sections.append(f"===== racadm getconfig -g {group} =====\n{body}\n")
    finally:
        client.close()

    header = (
        f"# iDRAC6 config backup\n"
        f"# host: {cfg['ip']}\n"
        f"# taken: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# groups: {', '.join(GROUPS)}\n"
        f"# NOTE: best-effort text snapshot for reference, not a restorable\n"
        f"# Lifecycle Controller export (iDRAC6 doesn't have that feature).\n\n"
    )
    with open(outpath, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(sections))

    print(f"Done. Wrote {outpath}")
    if empty_groups:
        print(f"WARNING: these groups returned no usable output, check {outpath} — "
              f"your firmware may use different group names, adjust GROUPS and re-run: "
              f"{', '.join(empty_groups)}")


if __name__ == "__main__":
    main()
