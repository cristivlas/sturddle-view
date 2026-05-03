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

## Slice 3 — Sanity checks

Resource check (`server/sturddle_view/tournament/rescheck.py`) lives in
its own module — no dependency on orchestrator state, callable from the
create-time endpoint and from `Orchestrator.start()` alike.

### Architecture

Client owns the **resolution** of per-engine settings (it has the engine
registry's `option_schema`); server owns the **comparison** against
local CPU / RAM (it knows the host).

Resolution (client, in `tournament-template-form.js`):

```
max_threads = template.engine_default_threads
              ?? max(resolved_threads(e) for e in selected_engines)
              ?? 1

max_hash_mb = template.engine_default_hash_mb
              ?? max(resolved_hash(e) for e in selected_engines)
              ?? 16

resolved_threads(e) = e.options.Threads ?? e.option_schema.Threads.default ?? 1
resolved_hash(e)    = e.options.Hash    ?? e.option_schema.Hash.default    ?? 16
```

These resolved values are folded into the template at create time so
the server can re-check at start without needing the registry. The
client also calls `POST /api/tournaments/preflight` with the same
values *before* hitting `POST /api/tournaments` — failure blocks the
create. `Orchestrator.start()` re-runs the same check against the
stored template (covers stale tournaments started after machine specs
changed, and direct-API misuse).

### Formula

CPU:

```
load = parallel * (2 if ponder else 1) * max_threads
```

RAM:

```
ram_load_mb = parallel * 2 * (max_hash_mb + ENGINE_OVERHEAD_MB)
```

`ENGINE_OVERHEAD_MB` = symbolic constant (initial guess ≈ 256 MB; covers
binary + NNUE weights + PV stacks for typical modern engines). Revisit
once we have telemetry.

### Block conditions

When `allow_oversubscribe = false`:
- `load > logical_cores`            → reason `"oversubscribed"`
- `ram_load_mb > 0.75 * total_ram`  → reason `"insufficient_ram"`

When `pin_affinity = true` (regardless of `allow_oversubscribe`):
- `load > physical_cores`           → reason `"affinity_exceeds_physical"`
  (affinity needs one physical core per slot — hyperthreading siblings
  won't satisfy it; flag wouldn't make this work even if user wanted)

When `allow_oversubscribe = true`: the CPU and RAM blocks become logged
warnings only. No hard ceiling — full user trust. Affinity check still
hard-blocks (it's a correctness gate, not a "this'll be slow" gate).

### Fairness (deferred — UI-only, not part of rescheck)
Per-engine UCI option asymmetry (Threads/Hash/Ponder differing across
the slate when no global override is in place) is a separate concern;
the client has all the data it needs. Surface as a soft warning in the
template form, not a block. Out of scope for rescheck.

### Cross-cutting noise warnings (deferred)
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
