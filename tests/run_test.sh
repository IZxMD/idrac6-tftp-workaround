#!/usr/bin/env bash
#
# End-to-end test for idrac_flash.py against the mock iDRAC in this folder.
# Generates a throwaway self-signed cert, starts the mock, and runs the real
# script against 127.0.0.1 with short poll intervals. No real hardware and no
# network access needed.
#
# Usage: tests/run_test.sh
#
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d)"
PORT=8443
PASS=0
FAIL=0

cleanup() { [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# Throwaway cert for localhost testing only.
openssl req -x509 -newkey rsa:2048 -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
    -days 1 -nodes -subj "/CN=localhost" >/dev/null 2>&1

cat > "$WORK/.env" <<EOF
ip:127.0.0.1
user:root
pw:test-pass-123
port:$PORT
EOF

# A small fake firmware image (content is irrelevant to the mock).
head -c 2000000 /dev/urandom > "$WORK/fake.d6"

# Fast intervals so the whole run takes seconds instead of minutes.
export IDRAC_STAGE_POLL=1 IDRAC_PROGRESS_POLL=1 IDRAC_REBOOT_POLL=1
export IDRAC_STAGE_TIMEOUT=15 IDRAC_PROGRESS_TIMEOUT=20 IDRAC_REBOOT_TIMEOUT=20

run_case() {
    local name="$1" scenario="$2" expect="$3"
    echo "============================================================"
    echo "CASE: $name (scenario=$scenario, expect exit $expect)"
    echo "============================================================"
    python3 "$HERE/mock_idrac.py" "$WORK/cert.pem" "$WORK/key.pem" "$PORT" "$scenario" \
        > "$WORK/mock.log" 2>&1 &
    MOCK_PID=$!
    sleep 1
    python3 "$ROOT/idrac_flash.py" "$WORK/.env" "$WORK/fake.d6"
    local got=$?
    kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""
    if [ "$got" -eq "$expect" ]; then
        echo ">>> PASS (exit $got)"; PASS=$((PASS+1))
    else
        echo ">>> FAIL (got exit $got, expected $expect)"; FAIL=$((FAIL+1))
        echo "--- mock log ---"; cat "$WORK/mock.log"
    fi
    echo ""
}

# Happy path: full flash, version verified -> exit 0
run_case "happy path" happy 0
# iDRAC rejects the commit -> script must fail cleanly -> exit 1
run_case "rejected commit" reject 1
# iDRAC comes back on the OLD version -> verification mismatch -> exit 1
run_case "version mismatch" mismatch 1

# --backup flag: idrac_flash.py should shell out to idrac_backup_config.py
# first (against a mock racadm/SSH shell) and only then flash. Needs paramiko;
# skipped if it's not installed, same as run_test_backup.sh.
echo "============================================================"
echo "CASE: --backup flag (expect exit 0)"
echo "============================================================"
SSH_PORT=2223
python3 -c "
import paramiko
key = paramiko.RSAKey.generate(2048)
key.write_private_key_file('$WORK/host_key.pem')
" 2>/dev/null && HAVE_PARAMIKO=1 || HAVE_PARAMIKO=0
if [ "$HAVE_PARAMIKO" -eq 1 ]; then
    cat > "$WORK/.env.backup" <<EOF
ip:127.0.0.1
user:root
pw:test-pass-123
port:$PORT
ssh_port:$SSH_PORT
EOF
    python3 "$HERE/mock_idrac.py" "$WORK/cert.pem" "$WORK/key.pem" "$PORT" happy \
        > "$WORK/mock.log" 2>&1 &
    MOCK_PID=$!
    python3 "$HERE/mock_ssh_idrac.py" "$WORK/host_key.pem" "$SSH_PORT" happy \
        > "$WORK/mock_ssh.log" 2>&1 &
    MOCK_SSH_PID=$!
    sleep 1
    (cd "$WORK" && python3 "$ROOT/idrac_flash.py" "$WORK/.env.backup" "$WORK/fake.d6" --backup)
    got=$?
    kill "$MOCK_PID" "$MOCK_SSH_PID" 2>/dev/null; wait "$MOCK_PID" "$MOCK_SSH_PID" 2>/dev/null; MOCK_PID=""
    if [ "$got" -eq 0 ] && [ -f "$WORK/idrac_config_backup.cfg" ]; then
        echo ">>> PASS (exit $got, backup file written)"; PASS=$((PASS+1))
    else
        echo ">>> FAIL (exit $got, backup file present: $([ -f "$WORK/idrac_config_backup.cfg" ] && echo yes || echo no))"; FAIL=$((FAIL+1))
    fi
else
    echo "paramiko not installed - skipping"
fi
echo ""

# Wrong password: mock is up (happy) but the env has a bad password -> exit 1
echo "============================================================"
echo "CASE: wrong password (expect exit 1)"
echo "============================================================"
python3 "$HERE/mock_idrac.py" "$WORK/cert.pem" "$WORK/key.pem" "$PORT" happy \
    > "$WORK/mock.log" 2>&1 &
MOCK_PID=$!
sleep 1
cat > "$WORK/.env.badpw" <<EOF
ip:127.0.0.1
user:root
pw:definitely-wrong
port:$PORT
EOF
python3 "$ROOT/idrac_flash.py" "$WORK/.env.badpw" "$WORK/fake.d6"
if [ $? -eq 1 ]; then echo ">>> PASS"; PASS=$((PASS+1)); else echo ">>> FAIL"; FAIL=$((FAIL+1)); fi
kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""
echo ""

# Failure paths that need no mock:
echo "============================================================"
echo "CASE: unreachable host (expect exit 1)"
echo "============================================================"
cat > "$WORK/.env.unreach" <<EOF
ip:127.0.0.1
user:root
pw:test-pass-123
port:9999
EOF
python3 "$ROOT/idrac_flash.py" "$WORK/.env.unreach" "$WORK/fake.d6"
if [ $? -eq 1 ]; then echo ">>> PASS"; PASS=$((PASS+1)); else echo ">>> FAIL"; FAIL=$((FAIL+1)); fi
echo ""

echo "============================================================"
echo "RESULT: $PASS passed, $FAIL failed"
echo "============================================================"
[ "$FAIL" -eq 0 ]
