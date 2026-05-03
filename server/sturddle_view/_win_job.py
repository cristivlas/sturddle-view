"""Windows Job Object helper.

Per-tournament Job (KILL_ON_JOB_CLOSE): atomic kill of fastchess + all
descendants when the Job handle closes. Use ``spawn_in_job`` to create
the process already inside the Job (no race), and ``close_job`` to
trigger the cascading kill.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

if sys.platform == "win32":
    _kernel32 = ctypes.windll.kernel32
    # ctypes restype defaults to c_int (32-bit); on 64-bit Windows that
    # truncates HANDLE return values. Declare explicitly.
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
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


# ---- Atomic spawn-into-Job via _winapi.CreateProcess interception ---------
#
# asyncio.create_subprocess_exec ultimately calls _winapi.CreateProcess.
# We monkey-patch it for the duration of one spawn so the new process is
# created already inside the Job (PROC_THREAD_ATTRIBUTE_JOB_LIST), with
# zero race window. asyncio still gets back the (hp, ht, pid, tid) tuple
# it expects from CreateProcess.

_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002

# PROC_THREAD_ATTRIBUTE_JOB_LIST requires Windows 10 build 14393
# (1607, "Anniversary Update", 2016). Older Windows can't atomic-spawn.
_ATOMIC_SPAWN_OK = (
    sys.platform == "win32"
    and sys.getwindowsversion().major >= 10
    and sys.getwindowsversion().build >= 14393
)
if sys.platform == "win32" and not _ATOMIC_SPAWN_OK:
    log.warning(
        "Windows < 10.0.14393 detected; atomic spawn-into-Job disabled, "
        "falling back to post-spawn AssignProcessToJobObject (small race)."
    )


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


if sys.platform == "win32":
    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    _kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel32.DeleteProcThreadAttributeList.restype = None
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION)]
    _kernel32.CreateProcessW.restype = wintypes.BOOL


def _build_attribute_list(job_handle: int, handle_list: list[int] | None):
    """Build PROC_THREAD attribute list with Job + (optional) handle list.
    Returns (raw_buffer, attr_list_ptr, kept_alive). Caller Delete+frees."""
    n_attrs = 1 + (1 if handle_list else 0)
    size = ctypes.c_size_t(0)
    _kernel32.InitializeProcThreadAttributeList(None, n_attrs, 0, ctypes.byref(size))
    buffer = (ctypes.c_byte * size.value)()
    attr_list = ctypes.cast(buffer, ctypes.c_void_p)
    if not _kernel32.InitializeProcThreadAttributeList(attr_list, n_attrs, 0, ctypes.byref(size)):
        raise ctypes.WinError()
    kept_alive: list = []
    job_h = wintypes.HANDLE(job_handle)
    kept_alive.append(job_h)
    if not _kernel32.UpdateProcThreadAttribute(
        attr_list, 0, _PROC_THREAD_ATTRIBUTE_JOB_LIST,
        ctypes.byref(job_h), ctypes.sizeof(job_h), None, None,
    ):
        err = ctypes.WinError()
        _kernel32.DeleteProcThreadAttributeList(attr_list)
        raise err
    if handle_list:
        arr_t = wintypes.HANDLE * len(handle_list)
        arr = arr_t(*handle_list)
        kept_alive.append(arr)
        if not _kernel32.UpdateProcThreadAttribute(
            attr_list, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.byref(arr), ctypes.sizeof(arr), None, None,
        ):
            err = ctypes.WinError()
            _kernel32.DeleteProcThreadAttributeList(attr_list)
            raise err
    return buffer, attr_list, kept_alive


def _create_process_in_job(
    job_handle, application_name, command_line, proc_attrs, thread_attrs,
    inherit_handles, creation_flags, env_mapping, current_directory, startup_info,
):
    """Drop-in replacement for ``_winapi.CreateProcess`` that creates the
    process inside ``job_handle`` atomically. Returns ``(hp, ht, pid, tid)``
    — same shape asyncio expects, with handles as int values."""
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOEXW)
    if startup_info is not None:
        si.dwFlags = getattr(startup_info, "dwFlags", 0)
        si.wShowWindow = getattr(startup_info, "wShowWindow", 0)
        si.hStdInput = getattr(startup_info, "hStdInput", None) or 0
        si.hStdOutput = getattr(startup_info, "hStdOutput", None) or 0
        si.hStdError = getattr(startup_info, "hStdError", None) or 0

    siex = _STARTUPINFOEXW()
    siex.StartupInfo = si
    # Preserve asyncio's handle_list (selective inheritance) so the
    # child still inherits the pipe handles.
    asyncio_attrs = getattr(startup_info, "lpAttributeList", None) or {}
    handle_list = list(asyncio_attrs.get("handle_list") or [])
    buffer, attr_list, _kept = _build_attribute_list(job_handle, handle_list)
    siex.lpAttributeList = attr_list

    pi = _PROCESS_INFORMATION()
    flags = creation_flags | _EXTENDED_STARTUPINFO_PRESENT

    # env mapping → contiguous wchar block "K=V\0K=V\0\0".
    env_block = None
    if env_mapping is not None:
        items = "".join(f"{k}={v}\0" for k, v in env_mapping.items())
        env_block = ctypes.create_unicode_buffer(items + "\0")
        env_ptr = ctypes.cast(env_block, ctypes.c_void_p)
    else:
        env_ptr = None

    cmd_buf = ctypes.create_unicode_buffer(command_line) if command_line else None
    ok = _kernel32.CreateProcessW(
        application_name, cmd_buf, proc_attrs, thread_attrs,
        bool(inherit_handles), flags | 0x00000400,  # CREATE_UNICODE_ENVIRONMENT
        env_ptr, current_directory,
        ctypes.cast(ctypes.byref(siex), ctypes.POINTER(_STARTUPINFOW)),
        ctypes.byref(pi),
    )
    if not ok:
        # Capture WinError BEFORE any other Win32 call clobbers GetLastError.
        err = ctypes.WinError()
        _kernel32.DeleteProcThreadAttributeList(attr_list)
        del buffer  # noqa: F841
        raise err
    _kernel32.DeleteProcThreadAttributeList(attr_list)
    del buffer  # noqa: F841 — kept alive until here on purpose
    return pi.hProcess, pi.hThread, pi.dwProcessId, pi.dwThreadId


class _SpawnInJob:
    """Context manager: monkey-patches _winapi.CreateProcess to spawn
    inside the given Job. Restores on exit. No-op off Windows."""

    def __init__(self, job_handle: int | None) -> None:
        self._job = job_handle
        self._original = None

    def __enter__(self):
        if self._job is None or not _ATOMIC_SPAWN_OK:
            return self
        import _winapi
        self._original = _winapi.CreateProcess
        job = self._job

        def patched(application_name, command_line, proc_attrs, thread_attrs,
                    inherit_handles, creation_flags, env_mapping,
                    current_directory, startup_info):
            return _create_process_in_job(
                job, application_name, command_line, proc_attrs, thread_attrs,
                inherit_handles, creation_flags, env_mapping,
                current_directory, startup_info,
            )

        _winapi.CreateProcess = patched
        return self

    def __exit__(self, *exc):
        if self._original is None:
            return
        import _winapi
        _winapi.CreateProcess = self._original
        self._original = None


def spawn_in_job(job_handle: int | None) -> _SpawnInJob:
    """``with spawn_in_job(h): proc = await create_subprocess_exec(...)``"""
    return _SpawnInJob(job_handle)
