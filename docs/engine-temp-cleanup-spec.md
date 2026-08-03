# Engine temp-file cleanup (minispec)

## Problem

Self-extracting engine builds (exe bundling NNUE weights, opening
book, etc.) unpack into the OS temp directory at startup. If the
engine crashes or we kill it (cancel grace expired, app shutdown),
the extracted files are orphaned in temp and accumulate forever.

## Approach

Own the temp location instead of chasing the extractor.

1. Managed root: `<user_data_dir>/engine-tmp/` (platformdirs, same
   base as imports).
2. Per-spawn subdir: `engine-tmp/<pid>-<token>/`, created just before
   spawn (pid is ours; token random -- child pid is unknown until
   after spawn).
3. Env injection in `_popen_kwargs`: set `TMP`, `TEMP` (Windows) and
   `TMPDIR` (POSIX) to the per-spawn subdir, overlaid on the parent
   env. User-provided per-engine env vars still win if they set the
   same keys.
4. On engine exit (quit, cancel-teardown, swap, throwaway cleanup):
   remove the subdir tree, best-effort. Failures log at warning and
   are retried by the startup sweep.
5. Orphan sweep at server startup: delete every subdir under
   `engine-tmp/`. Safe because no engine can be running before the
   server starts, and single-instance is enforced by `server.lock`.

## Coverage

- HvE play engine and AI-analysis throwaway engines: both spawn via
  `EngineSupervisor._open_uci` -> `_popen_kwargs`; covered.
- Engine probe (`probe_engine`): same `_popen_kwargs`; covered.
- Tournament engines run under fastchess, which spawns them itself;
  out of scope here. Optional follow-up: inject the same env vars
  into the fastchess subprocess so its engines inherit them.

## Limits

Extractors that ignore temp env vars (hardcoded `/tmp`, extract
next to the exe) are not covered. No known engine in use does this;
accepted risk.
