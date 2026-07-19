#!/usr/bin/env bash
#
# End-to-end test for idrac_backup_config.py against the mock SSH/racadm
# shell in this folder. No real hardware and no network access needed.
# Requires paramiko (pip install paramiko==2.11.0) for both the script
# and the mock server used here.
#
# Usage: tests/run_test_backup.sh
#
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d)"
PORT=2222
PASS=0
FAIL=0

cleanup() { [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

python3 -c "
import paramiko
key = paramiko.RSAKey.generate(2048)
key.write_private_key_file('$WORK/host_key.pem')
" || { echo "paramiko not installed (pip install paramiko==2.11.0) - skipping"; exit 0; }

cat > "$WORK/.env" <<EOF
ip:127.0.0.1
user:root
pw:test-pass-123
ssh_port:$PORT
EOF

run_case() {
    local name="$1" scenario="$2" expect="$3" grep_for="${4:-}" not_grep_for="${5:-}"
    echo "============================================================"
    echo "CASE: $name (scenario=$scenario, expect exit $expect)"
    echo "============================================================"
    python3 "$HERE/mock_ssh_idrac.py" "$WORK/host_key.pem" "$PORT" "$scenario" \
        > "$WORK/mock.log" 2>&1 &
    MOCK_PID=$!
    sleep 1
    out=$(python3 "$ROOT/idrac_backup_config.py" "$WORK/.env" "$WORK/backup.cfg" 2>&1)
    got=$?
    echo "$out"
    kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""
    ok=1
    [ "$got" -eq "$expect" ] || ok=0
    if [ -n "$grep_for" ]; then
        echo "$out" | grep -q "$grep_for" || ok=0
    fi
    if [ -n "$not_grep_for" ]; then
        echo "$out" | grep -q "$not_grep_for" && ok=0
    fi
    if [ "$ok" -eq 1 ]; then
        echo ">>> PASS (exit $got)"; PASS=$((PASS+1))
    else
        echo ">>> FAIL (got exit $got, expected $expect, expected to see: '$grep_for', expected NOT to see: '$not_grep_for')"; FAIL=$((FAIL+1))
    fi
    echo ""
}

# Happy path: every group returns data -> exit 0, no WARNING. This also
# covers the cfgUserAdmin indexed-group fix: if that regressed (plain -g
# call with no -i), the mock would return "ERROR:" and WARNING would appear.
run_case "happy path" happy 0 "" "WARNING"
# One group returns nothing -> still exit 0, but must WARN about it
run_case "partial output" partial 0 "WARNING"

# Slow output: cfgRacTuning is sent in two halves with a 3s gap (longer than
# the old 2.5s settle) before the prompt. The late half must still land in the
# backup file, i.e. the reader waited for the prompt instead of truncating.
echo "============================================================"
echo "CASE: slow blockwise output (expect exit 0, full capture)"
echo "============================================================"
python3 "$HERE/mock_ssh_idrac.py" "$WORK/host_key.pem" "$PORT" slow > "$WORK/mock.log" 2>&1 &
MOCK_PID=$!
sleep 1
python3 "$ROOT/idrac_backup_config.py" "$WORK/.env" "$WORK/backup_slow.cfg" > "$WORK/slow.out" 2>&1
got=$?
kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""
# cfgSerialTelnetEnable=0 is in the SECOND half of the cfgRacTuning reply.
if [ "$got" -eq 0 ] && grep -q "cfgSerialTelnetEnable=0" "$WORK/backup_slow.cfg" 2>/dev/null; then
    echo ">>> PASS (exit $got, late output captured)"; PASS=$((PASS+1))
else
    echo ">>> FAIL (exit $got, late output present: $(grep -q "cfgSerialTelnetEnable=0" "$WORK/backup_slow.cfg" 2>/dev/null && echo yes || echo no))"; FAIL=$((FAIL+1))
fi
echo ""

# Wrong password -> exit 1
echo "============================================================"
echo "CASE: wrong password (expect exit 1)"
echo "============================================================"
python3 "$HERE/mock_ssh_idrac.py" "$WORK/host_key.pem" "$PORT" happy > "$WORK/mock.log" 2>&1 &
MOCK_PID=$!
sleep 1
cat > "$WORK/.env.badpw" <<EOF
ip:127.0.0.1
user:root
pw:wrong-password
ssh_port:$PORT
EOF
python3 "$ROOT/idrac_backup_config.py" "$WORK/.env.badpw" "$WORK/backup.cfg"
got=$?
kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""
if [ "$got" -eq 1 ]; then echo ">>> PASS"; PASS=$((PASS+1)); else echo ">>> FAIL"; FAIL=$((FAIL+1)); fi
echo ""

# Unreachable host -> exit 1
echo "============================================================"
echo "CASE: unreachable host (expect exit 1)"
echo "============================================================"
cat > "$WORK/.env.unreach" <<EOF
ip:127.0.0.1
user:root
pw:test-pass-123
ssh_port:9999
EOF
python3 "$ROOT/idrac_backup_config.py" "$WORK/.env.unreach" "$WORK/backup.cfg"
got=$?
if [ "$got" -eq 1 ]; then echo ">>> PASS"; PASS=$((PASS+1)); else echo ">>> FAIL"; FAIL=$((FAIL+1)); fi
echo ""

echo "============================================================"
echo "RESULT: $PASS passed, $FAIL failed"
echo "============================================================"
[ "$FAIL" -eq 0 ]
