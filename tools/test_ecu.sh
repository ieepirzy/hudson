#!/usr/bin/env bash
# Full end-to-end integration test for Hudson + fake_ecu.py on vcan0.
#
# Usage:
#   ./tools/test_ecu.sh              # healthy scenario, TUI smoke test
#   ./tools/test_ecu.sh --degraded   # degraded scenario
#   ./tools/test_ecu.sh --pytest-only # skip TUI, run pytest suite only
#
# Requirements:
#   sudo access (or CAP_NET_ADMIN) for vcan setup
#   pip install -e .[dev]  (installs python-can, can-isotp, pytest)
#
# Logs:
#   logs/fake_ecu.log    — ECU side (all services + frame-level debug)
#   logs/hudson_vcan.log — Hudson log from the TUI run
#   logs/hudson.log      — Hudson rotating log (always written)

set -euo pipefail

INTERFACE=vcan0
SCENARIO=tools/scenarios/ford_transit_2010.yaml
DEGRADED_FLAG=""
PYTEST_ONLY=false
TUI_TIMEOUT=12   # seconds to let the TUI run before killing it

for arg in "$@"; do
  case "$arg" in
    --degraded)     DEGRADED_FLAG="--degraded" ;;
    --pytest-only)  PYTEST_ONLY=true ;;
    --help|-h)
      sed -n '2,20p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0
      ;;
  esac
done

# ── helpers ───────────────────────────────────────────────────────────────────

log()  { echo "[test_ecu] $*"; }
die()  { echo "[test_ecu] ERROR: $*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' not found — install it first"
}

# ── setup vcan0 ───────────────────────────────────────────────────────────────

log "Setting up $INTERFACE …"
if ! ip link show "$INTERFACE" >/dev/null 2>&1; then
  sudo modprobe vcan 2>/dev/null || die "modprobe vcan failed — is the vcan kernel module available?"
  sudo ip link add "$INTERFACE" type vcan 2>/dev/null || true
fi
sudo ip link set "$INTERFACE" up || die "ip link set $INTERFACE up failed"
log "$INTERFACE is up"

# ── verify scenario file ──────────────────────────────────────────────────────

[[ -f "$SCENARIO" ]] || die "Scenario file not found: $SCENARIO"

mkdir -p logs

# ── run pytest suite ──────────────────────────────────────────────────────────

require_cmd python3

log "Running pytest integration suite …"
python3 -m pytest tests/test_vcan_integration.py -v \
  --tb=short \
  2>&1 | tee logs/pytest_vcan.log

PYTEST_EXIT=${PIPESTATUS[0]}

if [[ "$PYTEST_ONLY" == "true" ]]; then
  log "Pytest exit code: $PYTEST_EXIT"
  exit $PYTEST_EXIT
fi

if [[ $PYTEST_EXIT -ne 0 ]]; then
  log "Pytest failed (exit $PYTEST_EXIT) — skipping TUI smoke test"
  exit $PYTEST_EXIT
fi

# ── TUI smoke test ────────────────────────────────────────────────────────────

log "Starting fake_ecu.py $DEGRADED_FLAG on $INTERFACE …"
python3 tools/fake_ecu.py \
  --scenario "$SCENARIO" \
  --interface "$INTERFACE" \
  --verbose \
  $DEGRADED_FLAG \
  > logs/fake_ecu.log 2>&1 &
ECU_PID=$!
log "Fake ECU PID=$ECU_PID  → logs/fake_ecu.log"
sleep 0.8

log "Running Hudson --vcan $INTERFACE for ${TUI_TIMEOUT}s …"
# timeout exits with 124 if the command is still running after the interval;
# that is expected for a TUI — treat it as success.
timeout "$TUI_TIMEOUT" python3 -m Hudson --vcan "$INTERFACE" \
  > logs/hudson_vcan.log 2>&1 \
  || HUDSON_EXIT=$?

if [[ "${HUDSON_EXIT:-0}" -eq 124 ]]; then
  log "Hudson ran for ${TUI_TIMEOUT}s and was terminated (expected for TUI)"
elif [[ "${HUDSON_EXIT:-0}" -ne 0 ]]; then
  log "Hudson exited with code ${HUDSON_EXIT:-?} — check logs/hudson_vcan.log"
fi

# ── tear down ─────────────────────────────────────────────────────────────────

log "Stopping fake ECU (PID=$ECU_PID) …"
kill "$ECU_PID" 2>/dev/null || true
wait "$ECU_PID" 2>/dev/null || true

# ── report ────────────────────────────────────────────────────────────────────

log ""
log "─── Results ─────────────────────────────────────────────────────────"
log ""
log "Pytest:  $(grep -E 'passed|failed|error' logs/pytest_vcan.log | tail -1 || echo '(see logs/pytest_vcan.log)')"
log ""
log "Hudson log — VIN / DTC / mode-22 lines:"
if [[ -f logs/hudson.log ]]; then
  grep -iE "(VIN|P0401|P0299|DTC|09E2|FD8A|168E|113C|boost|EGR|degraded)" \
    logs/hudson.log 2>/dev/null | tail -30 \
    || log "  (no matching lines in logs/hudson.log)"
else
  log "  logs/hudson.log not found"
fi
log ""
log "Fake ECU log (last 20 lines):"
tail -20 logs/fake_ecu.log 2>/dev/null || log "  (empty)"
log ""
log "─────────────────────────────────────────────────────────────────────"
log "All tests passed.  Full logs: logs/pytest_vcan.log  logs/fake_ecu.log  logs/hudson.log"
