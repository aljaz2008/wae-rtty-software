"""Process / system statistics with optional psutil and stdlib fallbacks (Windows 7 safe)."""
from __future__ import annotations

import os
import sys
import time

try:
    import psutil  # optional
    _PROC = psutil.Process()
    _PROC.cpu_percent(None)
    psutil.cpu_percent(None)
except Exception:  # noqa: BLE001
    psutil = None
    _PROC = None

_last = {"wall": time.perf_counter(), "cpu": time.process_time()}


def _rss_mb():
    if _PROC:
        return _PROC.memory_info().rss / 1e6
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        try:
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),
                                                     ctypes.byref(pmc), pmc.cb)
            return pmc.WorkingSetSize / 1e6
        except Exception:  # noqa: BLE001
            return None
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except Exception:  # noqa: BLE001
        return None


def process_stats():
    """CPU % of this program (of one core) since last call, memory, threads."""
    wall, cpu = time.perf_counter(), time.process_time()
    dw = wall - _last["wall"]
    cpu_pct = (cpu - _last["cpu"]) / dw * 100 if dw > 0.5 else None
    if dw > 0.5:
        _last.update(wall=wall, cpu=cpu)
    out = {"cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
           "rss_mb": round(_rss_mb() or 0, 1), "cores": os.cpu_count(),
           "python": sys.version.split()[0], "platform": sys.platform, "psutil": bool(psutil)}
    if psutil:
        try:
            vm = psutil.virtual_memory()
            out.update(sys_cpu_pct=psutil.cpu_percent(None), sys_ram_pct=vm.percent,
                       sys_ram_free_mb=round(vm.available / 1e6), threads=_PROC.num_threads())
        except Exception:  # noqa: BLE001
            pass
    return out
