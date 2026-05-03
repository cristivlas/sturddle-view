# Tournament Concurrency Improvements — Rough Plan

Status: draft. Update as slices land.

## Goals
- Let users opt into oversubscription (parallel > physical cores) safely.
- Optional CPU affinity for measurement-quality runs (SPRT).
- Preflight sanity checks + graceful crash handling for misconfigs.

## Slice 1 — Oversubscribe opt-in + crash handling
- Template field: `allow_oversubscribe: bool` (default false).
- `build_command` (fastchess.py): pass `-force-concurrency` (verify exact flag name) when true.
- UI: checkbox in tournament-template-form.js next to `games_in_parallel`.
- Preflight (orchestrator, before `runner.start`): error if `parallel > physical_cores` and flag off.
- Crash handling: enrich `runner_crash` payload with last N stderr lines. Currently `{rc}` only — supervisor in fastchess.py:493 should attach captured stderr tail.
- Test: e2e — set `parallel = cores+1` w/o override → tournament reaches crashed state with stderr snippet visible.

## Slice 2 — CPU affinity
- Template field: `pin_affinity: bool` (default false). Explicit, no auto-magic.
- `build_command`: pass fastchess `-affinity` flag when true.
- Preflight: if affinity on AND `parallel * threads > physical_cores` → error (affinity needs slot ≤ core).
- Tooltip: "Recommended for SPRT runs."
- Smoke test on Windows before shipping — verify `SetProcessAffinityMask` actually pins engine threads as expected (vs. just the parent).

## Slice 3 — Sanity checks (preflight)
Two independent categories, surfaced at create-time AND start-time.

### 3a. Resource preflight ("don't kill the machine")

Worst-case CPU load formula:

```
load = parallel * (2 if ponder else 1) * max_threads_setting
```

where `max_threads_setting` is the largest Threads value any single engine
uses during a game:

```
max_threads_setting =
    engine_default_threads                              # if set globally
    else max(engine.uci_threads_default for engine in engines)
```

Rationale: with ponder off, one engine searches per slot → `1 * max_threads`
CPUs busy. With ponder on, both engines search at once → `2 * max_threads`
busy. `max_threads_setting` defends against asymmetric per-engine defaults
when no global override is in place.

Errors (block start):
- `load > logical_cores` AND `allow_oversubscribe=false` (matches fastchess'
  own `-concurrency > nproc` rejection — see Empirical finding above).
- `pin_affinity=true` AND `load > physical_cores` (affinity needs slot ≤
  physical core; hyperthreading siblings won't satisfy it).

Warnings (informational):
- `parallel * 2 * hash_mb > 0.7 * system_ram` → paging risk (2 engines/slot,
  hash counted per-engine).
- TB on slow disk + high concurrency (stretch).

### 3b. Fairness preflight ("engines start on level ground")
Per-engine UCI options should not differ silently across the tournament
slate. When the global `engine_default_*` is set via `-each`, all engines
are forced equal → fair. When unset, the fall-back is each engine's
stored UCI defaults (already captured at registry add).

Warnings:
- `engine_default_threads is None` AND any two engines have differing
  Threads defaults → asymmetric compute.
- Same for `Hash`.
- `template.ponder=true` AND some engines don't advertise the `Ponder`
  UCI option → asymmetric (only ponder-aware engines benefit).
- `sprt` set AND any fairness warning fires → strongly recommend fixing
  before trusting the result.

### Cross-cutting noise warnings
- `sprt` set AND `parallel > 1` AND `pin_affinity=false` → measurement
  noisier than necessary.

## Open questions
- Exact fastchess flag names — verify against current fastchess version (`-force-concurrency` vs `--force-concurrency` vs other).
- Where to surface warnings in UI — start dialog confirmation? workspace banner?
- Cross-platform physical-core detection: `psutil.cpu_count(logical=False)` is the obvious pick; confirm available in deps.

## Findings & open bugs (live)

### Empirical: fastchess gates at LOGICAL cpu count (Windows; Linux TBD)
- Earlier "doesn't gate" conclusion was wrong — proxy bug was masking the real failure mode.
- Real behavior on Win box (8 physical / 16 logical): `parallel <= 16` accepted, `parallel = 17` refused with
  `Concurrency exceeds number of CPUs. Use -force-concurrency to override.`
- So default permits SMT oversubscription up to logical core count.
- Slice 1 priority drops: overriding past logical CPU count is niche. Implement only if a user actually wants it.
- TODO: repro on Linux to confirm same threshold (some fastchess builds may differ).

### RESOLVED — uciok timeout was the proxy on Windows (commit `8bceac3`)
- Symptom: `Fatal; ... uciok after startup` reproducibly even at parallel=1.
- Root cause: `proxy.py` used `loop.connect_read_pipe(sys.stdin)`. On Windows + ProactorEventLoop, the inherited stdin handle is non-overlapped, so the reader silently never delivers data — proxy never forwards `uci`, engine never replies `uciok`.
- Fix: Windows-only daemon thread doing blocking `sys.stdin.buffer.readline()` + `loop.call_soon_threadsafe(reader.feed_data, line)`.

### RESOLVED — orphan engines + wedged proc.wait on Stop (commit `37edcc4`)
- Symptom: clicking Stop, fastchess died (verified via `Get-CimInstance`), but `proc.wait()` in supervisor never returned. State stuck on `running`. 16+ engine + proxy processes orphaned each run.
- Root cause: ProactorEventLoop's process-exit detection wedges when child descendants hold inherited stdio pipe handles (engines + proxies survived fastchess's death since fastchess died via TerminateProcess without reaping them).
- Fix: per-tournament Windows Job Object (`_win_job.create_job` + `assign_to_job` + `close_job`). Stop closes the Job handle → `KILL_ON_JOB_CLOSE` synchronously kills fastchess + every descendant → asyncio's wait resolves cleanly. Supervisor closes the Job on natural termination too.
- Also obsoletes the brief `_force_terminal_event` workaround we tried.

### Stale-running reconciliation (still open)
- On server boot, `STATUS_RUNNING` rows currently get reconciled to `STATUS_STOPPED`. Should be `STATUS_FAILED` with synthetic `last_error = {"reason": "server crashed/killed mid-tournament"}`.

### Watch-window silence (resolved by proxy fix above)
- Proxy now actually forwards UCI lines on Windows; live windows should populate.

### Slice 5 — Reframe oversubscription gate (was Slice 1's premise)
- fastchess accepts oversubscription silently → gate is no longer "block hard error".
- New purpose: warn user that high concurrency degrades Elo measurement (SMT contention + startup races).
- UI shape: probably a soft warning at create time when `parallel > physical_cores`, not a hard block.

### TODO: atomic spawn-into-Job (lower priority polish)
- Today: assign-after-spawn has a tiny race where a child fastchess spawns BEFORE we call `AssignProcessToJobObject` escapes the Job. fastchess does ms of setup before forking engines, so observable race ≈ 0.
- Proper fix: `PROC_THREAD_ATTRIBUTE_JOB_LIST` (Windows 10+) via ctypes `CreateProcess`. Requires bypassing `asyncio.create_subprocess_exec` for fastchess.
