"""Windows Job Object helper.

Per-tournament Job pattern: caller creates a Job for each fastchess
spawn (configured with KILL_ON_JOB_CLOSE), assigns fastchess to it,
and closes the handle when the tournament terminates. Closing the
handle synchronously kills fastchess + every descendant in the Job
(engines + proxies), avoiding orphans and inherited-pipe-handle
issues that wedge asyncio's process-exit detection.

Race note: a child spawned by the assigned process before
AssignProcessToJobObject runs is NOT in the Job. fastchess spawns
engines only after some startup work, so the window is small.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

if sys.platform == "win32":
    _kernel32 = ctypes.windll.kernel32
else:
    _kernel32 = None

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_TERMINATE_AND_SET_QUOTA = 0x0001 | 0x0100  # plenty for assignment


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def create_job() -> int | None:
    """Create a fresh Job Object configured with KILL_ON_JOB_CLOSE.
    Returns the Job handle, or None off Windows."""
    if sys.platform != "win32":
        return None
    h = _kernel32.CreateJobObjectW(None, None)
    if not h:
        raise ctypes.WinError()
    info = _EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
        h, _JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        err = ctypes.WinError()
        _kernel32.CloseHandle(h)
        raise err
    return h


def assign_to_job(job_handle: int | None, pid: int) -> None:
    """Add pid to the given Job. No-op if job_handle is None."""
    if job_handle is None or sys.platform != "win32":
        return
    h_proc = _kernel32.OpenProcess(_PROCESS_TERMINATE_AND_SET_QUOTA, False, pid)
    if not h_proc:
        raise ctypes.WinError()
    try:
        if not _kernel32.AssignProcessToJobObject(job_handle, h_proc):
            raise ctypes.WinError()
    finally:
        _kernel32.CloseHandle(h_proc)


def close_job(job_handle: int | None) -> None:
    """Close the Job handle. Triggers KILL_ON_JOB_CLOSE — every process
    in the Job is killed synchronously by the OS. Idempotent on None."""
    if job_handle is None or sys.platform != "win32":
        return
    if not _kernel32.CloseHandle(job_handle):
        log.warning("close_job: CloseHandle failed (errno=%d)",
                    ctypes.get_last_error())
