"""Windows Job Object helper.

A single process-wide Job is created lazily on first ``assign_pid``.
Configured with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so when our
Python process exits (clean or via os._exit), Windows tears down every
process in the Job — fastchess and the UCI engines + proxies it spawned.

Race note: a child spawned by the assigned process before we call
AssignProcessToJobObject is NOT in the Job. fastchess spawns engines
only after some startup work, so the window is small but non-zero.
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

_JOB_HANDLE: int | None = None

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


def _get_or_create_job() -> int:
    global _JOB_HANDLE
    if _JOB_HANDLE is not None:
        return _JOB_HANDLE
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
    _JOB_HANDLE = h
    return h


def assign_pid(pid: int) -> None:
    """Add the given pid to the process-wide Job. No-op off Windows.

    Children the process spawns AFTER this call inherit the Job and
    are killed when our Python process exits.
    """
    if sys.platform != "win32":
        return
    h_proc = _kernel32.OpenProcess(_PROCESS_TERMINATE_AND_SET_QUOTA, False, pid)
    if not h_proc:
        raise ctypes.WinError()
    try:
        if not _kernel32.AssignProcessToJobObject(_get_or_create_job(), h_proc):
            raise ctypes.WinError()
    finally:
        _kernel32.CloseHandle(h_proc)
