# Tournament Concurrency Improvements — Rough Plan

Status: draft. Update as slices land.

## Goals
- Let users opt into oversubscription (parallel > physical cores) safely.
- Optional CPU affinity for measurement-quality runs (SPRT).
- Preflight sanity checks + graceful crash handling for misconfigs.

## Slice 1 — Oversubscribe opt-in + crash handling — DONE
- `allow_oversubscribe` template field + UI switch (Slice 3 commit).
- `build_command` emits `-force-concurrency` when the flag is on.
- `runner_crash` carries `stderr_tail`; surfaced in toast/banner/info dialog.
- Resource gating happens via the rescheck path (Slice 3), not a
  parallel-only check.

## Slice 2 — CPU affinity — DONE
- `pin_affinity` template field + UI switch.
- `build_command` emits `-affinity`.
- Rescheck blocks `pin_affinity AND load > physical_cores` (correctness
  gate, never silenced by `allow_oversubscribe`).

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
client also calls `POST /api/tournaments/rescheck` with the same
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
- Where to surface rescheck warnings in UI — currently a toast at create
  time. Stickier alternatives: workspace banner, info dialog row.

## Findings & open bugs (live)

### Empirical: fastchess gates at LOGICAL cpu count
- On Win 8-physical/16-logical box: `parallel <= 16` accepted, `parallel
  = 17` refused with `Concurrency exceeds number of CPUs. Use
  -force-concurrency to override.`
- We pass `-force-concurrency` whenever `allow_oversubscribe` is on so
  fastchess matches our rescheck verdict.
- Linux behavior: TBD. Same threshold is likely but not confirmed.

### RESOLVED — uciok timeout was the proxy on Windows (commit `8bceac3`)
- Symptom: `Fatal; ... uciok after startup` reproducibly even at parallel=1.
- Root cause: `proxy.py` used `loop.connect_read_pipe(sys.stdin)`. On Windows + ProactorEventLoop, the inherited stdin handle is non-overlapped, so the reader silently never delivers data — proxy never forwards `uci`, engine never replies `uciok`.
- Fix: Windows-only daemon thread doing blocking `sys.stdin.buffer.readline()` + `loop.call_soon_threadsafe(reader.feed_data, line)`.

### RESOLVED — orphan engines + wedged proc.wait on Stop (commit `37edcc4`)
- Symptom: clicking Stop, fastchess died (verified via `Get-CimInstance`), but `proc.wait()` in supervisor never returned. State stuck on `running`. 16+ engine + proxy processes orphaned each run.
- Root cause: ProactorEventLoop's process-exit detection wedges when child descendants hold inherited stdio pipe handles (engines + proxies survived fastchess's death since fastchess died via TerminateProcess without reaping them).
- Fix: per-tournament Windows Job Object (`_win_job.create_job` + `assign_to_job` + `close_job`). Stop closes the Job handle → `KILL_ON_JOB_CLOSE` synchronously kills fastchess + every descendant → asyncio's wait resolves cleanly. Supervisor closes the Job on natural termination too.
- Also obsoletes the brief `_force_terminal_event` workaround we tried.

### RESOLVED — stale-running reconciliation
- Server-boot `STATUS_RUNNING` rows now flip to `STATUS_FAILED` with a
  synthetic `last_error` (see `Orchestrator.reconcile_on_startup`).

### Deferred: SMT-noise warning
- Even when `parallel <= logical_cores`, anything above `physical_cores`
  uses SMT siblings → measurable noise on SPRT runs.
- UI shape: soft warning at create time when `parallel > physical_cores
  AND !pin_affinity`, surfaced as a toast.
