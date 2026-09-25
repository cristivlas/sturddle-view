"""Resource sanity checks for tournament starts.

Pure: no orchestrator state, no I/O. Called from the create-time
rescheck endpoint and from ``Orchestrator.start()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import psutil

from ..env_utils import env_float, env_int
from ..error_detail import MESSAGE_KEY, REASON_KEY
from .template import (
    ALLOW_OVERSUBSCRIBE_KEY,
    GAMES_IN_PARALLEL_KEY,
    MAX_HASH_MB_KEY,
    MAX_THREADS_KEY,
    PIN_AFFINITY_KEY,
    PONDER_KEY,
)

# Per-engine memory beyond the configured Hash table: binary, NNUE/eval
# weights, PV stacks, search overhead. Symbolic -- revisit with telemetry.
ENGINE_OVERHEAD_MB = env_int("SV_RESCHECK_ENGINE_OVERHEAD_MB", 256, min_value=0)
# Share of total RAM the tournament's engines may use.
RAM_HEADROOM_FACTOR = env_float("SV_RESCHECK_RAM_HEADROOM", 0.75, min_value=0.0)

# Hash size assumed when neither the request nor the template names one.
DEFAULT_HASH_MB = 16

# Every game runs two engine processes; pondering doubles each one's CPU.
_ENGINES_PER_GAME = 2
_PONDER_CPU_FACTOR = 2
_BYTES_PER_MB = 1024 * 1024

_REASON_OVERSUBSCRIBED = "oversubscribed"
_REASON_INSUFFICIENT_RAM = "insufficient_ram"


@dataclass
class RescheckError(Exception):
    """Raised when a tournament's resource budget can't be honored.
    ``reason`` is a stable token; ``message`` is the user-facing string;
    ``details`` carries the numbers for the UI to render alongside."""
    reason: str
    message: str
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass
class HostSpecs:
    logical_cores: int
    physical_cores: int
    total_ram_mb: int


def host_specs() -> HostSpecs:
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    total_ram_mb = int(psutil.virtual_memory().total // _BYTES_PER_MB)
    return HostSpecs(logical, physical, total_ram_mb)


def _warning(reason: str, message: str, details: dict) -> dict:
    return {REASON_KEY: reason, MESSAGE_KEY: message, **details}


def check(
    *,
    parallel: int,
    max_threads: int,
    max_hash_mb: int,
    ponder: bool = False,
    pin_affinity: bool = False,
    allow_oversubscribe: bool = False,
    specs: HostSpecs | None = None,
) -> list[dict]:
    """Run all resource checks. Raises ``RescheckError`` on a hard block;
    returns a list of warning dicts for soft conditions (currently those
    that would have blocked but were suppressed by ``allow_oversubscribe``).
    """
    if specs is None:
        specs = host_specs()

    parallel = max(1, int(parallel))
    max_threads = max(1, int(max_threads))
    max_hash_mb = max(0, int(max_hash_mb))

    cpu_load = parallel * (_PONDER_CPU_FACTOR if ponder else 1) * max_threads
    ram_load_mb = parallel * _ENGINES_PER_GAME * (max_hash_mb + ENGINE_OVERHEAD_MB)
    ram_budget_mb = int(specs.total_ram_mb * RAM_HEADROOM_FACTOR)

    affinity_load = parallel * _ENGINES_PER_GAME * max_threads
    base = {
        "parallel": parallel,
        MAX_THREADS_KEY: max_threads,
        MAX_HASH_MB_KEY: max_hash_mb,
        PONDER_KEY: ponder,
        "cpu_load": cpu_load,
        "affinity_load": affinity_load,
        "ram_load_mb": ram_load_mb,
        "ram_budget_mb": ram_budget_mb,
        "logical_cores": specs.logical_cores,
        "physical_cores": specs.physical_cores,
        "total_ram_mb": specs.total_ram_mb,
    }
    warnings: list[dict] = []

    # Affinity pins each engine *process* to dedicated physical cores; always
    # 2 processes per game regardless of ponder.
    if pin_affinity and affinity_load > specs.physical_cores:
        raise RescheckError(
            reason="affinity_exceeds_physical",
            message=(
                f"CPU affinity: need {affinity_load} physical cores, "
                f"have {specs.physical_cores}. Lower parallelism/Threads or disable Affinity."
            ),
            details=base,
        )

    if cpu_load > specs.logical_cores:
        msg = (
            f"Worst-case CPU load {cpu_load} exceeds {specs.logical_cores} "
            f"logical cores."
        )
        if allow_oversubscribe:
            warnings.append(_warning(_REASON_OVERSUBSCRIBED, msg, base))
        else:
            raise RescheckError(
                reason=_REASON_OVERSUBSCRIBED,
                message=(
                    f"{msg} Lower parallelism/Threads/Ponder, or enable "
                    f"oversubscription explicitly."
                ),
                details=base,
            )

    if ram_load_mb > ram_budget_mb:
        msg = (
            f"Estimated RAM use {ram_load_mb} MB exceeds {ram_budget_mb} MB "
            f"({RAM_HEADROOM_FACTOR:.0%} of {specs.total_ram_mb} MB total)."
        )
        if allow_oversubscribe:
            warnings.append(_warning(_REASON_INSUFFICIENT_RAM, msg, base))
        else:
            raise RescheckError(
                reason=_REASON_INSUFFICIENT_RAM,
                message=(
                    f"{msg} Lower Hash size, lower parallelism, or enable "
                    f"oversubscription explicitly."
                ),
                details=base,
            )

    return warnings


def check_template(template: dict, *, specs: HostSpecs | None = None) -> list[dict]:
    """Convenience wrapper for ``Orchestrator.start()`` -- pulls the
    resolved values the client folded into the template at create time.
    Missing values fall back to single-thread / 16 MB hash so legacy
    tournaments (created before rescheck existed) still launch."""
    return check(
        parallel=int(template.get(GAMES_IN_PARALLEL_KEY, 1) or 1),
        max_threads=int(template.get(MAX_THREADS_KEY, 1) or 1),
        max_hash_mb=int(template.get(MAX_HASH_MB_KEY, DEFAULT_HASH_MB) or DEFAULT_HASH_MB),
        ponder=bool(template.get(PONDER_KEY)),
        pin_affinity=bool(template.get(PIN_AFFINITY_KEY)),
        allow_oversubscribe=bool(template.get(ALLOW_OVERSUBSCRIBE_KEY)),
        specs=specs,
    )
