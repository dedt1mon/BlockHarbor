"""Choose a Java heap ceiling, leaving room for native memory and the OS."""

import ctypes
import sys
from pathlib import Path


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("total", ctypes.c_uint64),
        ("available", ctypes.c_uint64),
        ("total_page", ctypes.c_uint64),
        ("available_page", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("available_virtual", ctypes.c_uint64),
        ("extended", ctypes.c_uint64),
    ]


def physical_memory():
    """Return total and currently available physical RAM in MiB, if available."""
    if sys.platform == "win32":
        info = MemoryStatus()
        info.length = ctypes.sizeof(info)
        query = ctypes.windll.kernel32.GlobalMemoryStatusEx
        query.argtypes = [ctypes.POINTER(MemoryStatus)]
        query.restype = ctypes.c_int
        if query(ctypes.byref(info)):
            return info.total >> 20, info.available >> 20
    elif sys.platform.startswith("linux"):
        try:
            items = dict(
                line.split(":", 1)
                for line in Path("/proc/meminfo").read_text().splitlines()
            )
            return tuple(
                int(items[name].split()[0]) // 1024
                for name in ("MemTotal", "MemAvailable")
            )
        except (OSError, KeyError, ValueError, IndexError):
            pass
    return None


def automatic_heap():
    memory = physical_memory()
    if memory is None:
        return 2048
    total, available = memory
    # Do not commit this ceiling up front. JVM native memory, rendering and other
    # programs share physical RAM with the heap. Buckets keep Prism profiles stable.
    budget = min(total // 2, available * 2 // 3)
    return next(
        (size for size in (6144, 4096, 3072, 2048, 1536) if size <= budget), 1024
    )
