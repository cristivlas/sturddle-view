#!/usr/bin/env bash
# Nightly cosmic-ray run over load-bearing HVE + tournament modules.
# Serial, one session per module. Writes per-module HTML + a top-level summary.
#
# Usage:
#   bash scripts/cosmic-ray-nightly.sh
#   nohup bash scripts/cosmic-ray-nightly.sh > /dev/null 2>&1 &
#
# Output:   cosmic-ray-runs/<timestamp>/
#   run.log               (full stdout/stderr)
#   summary.txt           (per-module kill rate + survivor digest)
#   <module>.sqlite       (raw session)
#   <module>.html         (browsable report)
#   <module>.toml         (config used)
set -u
cd "$(dirname "$0")/.."

TS=$(date +%Y%m%d-%H%M%S)
OUT="cosmic-ray-runs/${TS}"
mkdir -p "${OUT}"
LOG="${OUT}/run.log"

# Per-module timeout (seconds for each mutant test run).
# Whole test suite is too slow to use as baseline; we narrow per module.
# A clean baseline of the largest narrow set takes ~8s; 60s gives 7x headroom
# for a mutant that wedges before its own timeout fires.
MUTANT_TIMEOUT=60.0
# Hard cap per module. 2h each = 10h worst case for 5 modules.
MODULE_BUDGET_SEC=7200

# module -> space-separated list of test files to run for it. Narrow so each
# mutant test run takes seconds, not minutes. e2e tests are excluded.
declare -A MODULE_TESTS
MODULE_TESTS["server/sturddle_view/play/human_vs_engine.py"]="server/tests/test_pgn_export.py server/tests/test_play_mode_fsm.py server/tests/test_view_mode.py server/tests/test_view_start_and_edit_recents.py server/tests/test_hve_engine_defaults.py server/tests/test_recents_play_save.py"
MODULE_TESTS["server/sturddle_view/tournament/orchestrator.py"]="server/tests/test_tournament_orchestrator.py server/tests/test_tournament_pairing.py server/tests/test_tournament_game_lifecycle.py"
MODULE_TESTS["server/sturddle_view/tournament/pgn_reconcile.py"]="server/tests/test_pgn_reconcile_queue.py"
MODULE_TESTS["server/sturddle_view/tournament/pgn_tail.py"]="server/tests/test_pgn_tail.py"
MODULE_TESTS["server/sturddle_view/tournament/pgn_stats.py"]="server/tests/test_tournament_pgn_stats.py"

MODULES=(
  "server/sturddle_view/play/human_vs_engine.py"
  "server/sturddle_view/tournament/orchestrator.py"
  "server/sturddle_view/tournament/pgn_reconcile.py"
  "server/sturddle_view/tournament/pgn_tail.py"
  "server/sturddle_view/tournament/pgn_stats.py"
)

log()    { echo "[$(date +%H:%M:%S)] $*" | tee -a "${LOG}"; }
header() { echo                                  | tee -a "${LOG}"
           echo "============================================================" | tee -a "${LOG}"
           echo "$*"                             | tee -a "${LOG}"
           echo "============================================================" | tee -a "${LOG}"; }

header "cosmic-ray nightly  ${TS}"
log "host: $(hostname)  python: $(python -V 2>&1)  cosmic-ray: $(cosmic-ray --version 2>&1)"
log "output: ${OUT}"
log "modules: ${#MODULES[@]}"

OVERALL_START=$(date +%s)

for MODULE in "${MODULES[@]}"; do
  SLUG=$(echo "${MODULE}" | sed 's|.*/||; s|\.py$||')
  SESSION="${OUT}/${SLUG}.sqlite"
  HTML="${OUT}/${SLUG}.html"
  CONFIG="${OUT}/${SLUG}.toml"

  header "module: ${MODULE}"

  TESTS="${MODULE_TESTS[${MODULE}]}"
  if [ -z "${TESTS}" ]; then
    log "  no test mapping; skipping"
    continue
  fi
  cat > "${CONFIG}" <<EOF
[cosmic-ray]
module-path = "${MODULE}"
timeout = ${MUTANT_TIMEOUT}
excluded-modules = []
test-command = "python -m pytest -x -q --no-header ${TESTS}"

[cosmic-ray.distributor]
name = "local"
EOF

  log "init session ${SESSION}"
  if ! cosmic-ray init "${CONFIG}" "${SESSION}" >> "${LOG}" 2>&1; then
    log "  init failed; skipping"
    continue
  fi
  COUNT=$(python3 -c "
import sqlite3, sys
con=sqlite3.connect('${SESSION}')
print(con.execute('SELECT COUNT(*) FROM work_items').fetchone()[0])
")
  log "  ${COUNT} mutants generated"

  log "baseline check"
  if ! timeout 120 cosmic-ray baseline "${CONFIG}" >> "${LOG}" 2>&1; then
    log "  baseline FAILED -- module skipped (test suite must pass clean first)"
    continue
  fi

  log "exec (budget ${MODULE_BUDGET_SEC}s)"
  MOD_START=$(date +%s)
  timeout "${MODULE_BUDGET_SEC}" cosmic-ray exec "${CONFIG}" "${SESSION}" >> "${LOG}" 2>&1
  RC=$?
  MOD_ELAPSED=$(( $(date +%s) - MOD_START ))
  if [ ${RC} -eq 124 ]; then
    log "  TIMEOUT after ${MOD_ELAPSED}s; using partial results"
  elif [ ${RC} -ne 0 ]; then
    log "  exec exited rc=${RC} after ${MOD_ELAPSED}s; using partial results"
  else
    log "  done in ${MOD_ELAPSED}s"
  fi

  # cosmic-ray patches the source file in place; if `timeout` SIGTERM'd
  # the worker between patch-apply and patch-restore, the file is left
  # mutated. Restore from HEAD if dirty.
  if ! git diff --quiet -- "${MODULE}"; then
    log "  mutant residue detected in ${MODULE}; restoring from HEAD"
    git checkout HEAD -- "${MODULE}" >> "${LOG}" 2>&1
  fi

  log "rendering report"
  cr-html "${SESSION}" > "${HTML}" 2>>"${LOG}" || log "  cr-html failed"
done

# ---------- summary ----------
header "summary"
SUMMARY="${OUT}/summary.txt"
{
  echo "cosmic-ray nightly ${TS}"
  echo "total wall: $(( $(date +%s) - OVERALL_START ))s"
  echo
  for MODULE in "${MODULES[@]}"; do
    SLUG=$(echo "${MODULE}" | sed 's|.*/||; s|\.py$||')
    SESSION="${OUT}/${SLUG}.sqlite"
    [ -f "${SESSION}" ] || { echo "${SLUG}: (no session)"; continue; }
    python3 - "${SESSION}" "${MODULE}" <<'PY'
import sqlite3, sys
from collections import Counter
sess, module = sys.argv[1], sys.argv[2]
con = sqlite3.connect(sess)
cur = con.cursor()
cur.execute("SELECT COUNT(*) FROM work_items")
total = cur.fetchone()[0]
cur.execute("SELECT test_outcome, COUNT(*) FROM work_results GROUP BY test_outcome")
out = dict(cur.fetchall())
done = sum(out.values())
killed = out.get('KILLED', 0)
survived = out.get('SURVIVED', 0)
incomplete = total - done
rate = (killed / done * 100) if done else 0.0
print(f"{module}")
print(f"  mutants={total}  done={done}  killed={killed}  survived={survived}  incomplete={incomplete}  kill_rate={rate:.1f}%")

# top-survivor digest: per-function survival counts
cur.execute("""
SELECT ms.definition_name, COUNT(*) AS n
FROM mutation_specs ms JOIN work_results wr ON ms.job_id = wr.job_id
WHERE wr.test_outcome = 'SURVIVED'
GROUP BY ms.definition_name
ORDER BY n DESC LIMIT 10
""")
rows = cur.fetchall()
if rows:
    print("  top survivors (function, count):")
    for fn, n in rows:
        print(f"    {fn or '<module>':40s} {n}")
print()
PY
  done
} | tee "${SUMMARY}" | tee -a "${LOG}"

log "complete -- summary at ${SUMMARY}"
