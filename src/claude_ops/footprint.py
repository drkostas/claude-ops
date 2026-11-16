"""footprint — a process's memory footprint, the figure top shows as MEM, read with proc_pid_rusage.

phys_footprint counts compressed pages, as top's MEM does, and reading it for one process takes
about a microsecond, so a cap can watch a process tree every few seconds without asking `top`.

Only this user's processes: proc_pid_rusage refuses another user's process, so footprint_gb says
None for it, never a guess. macOS only.
"""
from __future__ import annotations

import ctypes
import ctypes.util


class _RusageInfoV2(ctypes.Structure):
    """libproc's rusage_info_v2 (sys/resource.h): a uuid, then 64-bit counters in this order."""
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime", "ri_child_user_time", "ri_child_system_time",
        "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups", "ri_child_pageins",
        "ri_child_elapsed_abstime", "ri_diskio_bytesread", "ri_diskio_byteswritten")]


_libproc = None


def _usage(pid: int) -> _RusageInfoV2 | None:
    """proc_pid_rusage's counters for one process, or None for a process that is gone or another user's,
    and on a system that has no libproc."""
    global _libproc
    if _libproc is None:
        try:
            _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        except OSError:          # not macOS: there is no proc_pid_rusage to ask
            _libproc = False
    if _libproc is False:
        return None
    info = _RusageInfoV2()
    if _libproc.proc_pid_rusage(int(pid), 2, ctypes.byref(info)) != 0:     # RUSAGE_INFO_V2
        return None
    return info


def footprint_gb(pid: int) -> float | None:
    """A process's memory footprint in GB, the figure top shows as MEM (phys_footprint, which counts
    compressed pages), from proc_pid_rusage in about a microsecond. None for a process that is gone
    or another user's."""
    info = _usage(pid)
    return None if info is None else info.ri_phys_footprint / 1024 ** 3


def resident_gb(pid: int) -> float | None:
    """How much of a process is in memory now, in GB (resident_size, the figure ps shows as RSS). Pages
    the system swapped out or compressed are not in it, so a process whose resident size falls far below
    its footprint is mostly waiting on swap. None for a process that is gone or another user's."""
    info = _usage(pid)
    return None if info is None else info.ri_resident_size / 1024 ** 3


def total_gb(pids) -> float:
    """The footprints of these processes added up, in GB; a process that is gone counts nothing."""
    return sum(gb for pid in pids if (gb := footprint_gb(pid)) is not None)


def total_resident_gb(pids) -> float:
    """The resident sizes of these processes added up, in GB; a process that is gone counts nothing."""
    return sum(gb for pid in pids if (gb := resident_gb(pid)) is not None)
