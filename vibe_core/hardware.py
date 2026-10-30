"""Hardware detection, RAM recommendations and performance presets.

Honesty note: the biggest real-world FPS gains in Minecraft come from rendering
mods (Sodium) and lower render/simulation distance. JVM flags mainly affect
garbage-collection pauses (stutter), not average FPS, and more RAM than the game
needs does not raise FPS. Presets are written with that in mind.
"""
import os
import platform
import re
import subprocess
import sys

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def _run(cmd, timeout=8):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=_NO_WINDOW)
        return out.stdout
    except Exception:
        return ""


# ------------------------------------------------------------------- RAM ---
def ram_mb():
    """(total_mb, available_mb), or (0, 0) if unknown."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys // 2**20, st.ullAvailPhys // 2**20
        elif sys.platform == "darwin":
            total = int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0) // 2**20
            vm = _run(["vm_stat"])
            page = int((re.search(r"page size of (\d+)", vm) or [0, 4096])[1])
            free = sum(int(m) for m in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)", vm))
            return total, free * page // 2**20
        else:
            with open("/proc/meminfo") as f:
                info = f.read()
            total = int(re.search(r"MemTotal:\s+(\d+)", info).group(1)) // 1024
            avail = re.search(r"MemAvailable:\s+(\d+)", info)
            return total, (int(avail.group(1)) // 1024) if avail else 0
    except Exception:
        pass
    return 0, 0


def cpu_name():
    try:
        if sys.platform == "win32":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        if sys.platform == "darwin":
            return _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or platform.processor()
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"


def gpu_names():
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"])
    elif sys.platform == "darwin":
        out = "\n".join(re.findall(r"Chipset Model:\s*(.+)", _run(["system_profiler", "SPDisplaysDataType"], 15)))
    else:
        out = "\n".join(re.sub(r"^.*?: ", "", l) for l in _run(["lspci"]).splitlines()
                        if re.search(r"VGA|3D controller|Display", l))
    names = [l.strip() for l in out.splitlines() if l.strip()]
    # Drop virtual adapters that don't render the game.
    return [n for n in names if not re.search(r"Basic Display|Remote Display|Virtual|Parsec|Meta Virtual", n, re.I)]


_INTEGRATED = re.compile(r"Intel\(R\) (UHD|HD|Iris)|Intel.*(UHD|HD Graphics|Iris)|AMD Radeon\(TM\) Graphics|"
                         r"Radeon Vega \d+ Graphics|Radeon\(TM\) \d+M|Apple M\d", re.I)


def is_integrated(gpu: str) -> bool:
    return bool(_INTEGRATED.search(gpu or ""))


def detect():
    total, avail = ram_mb()
    gpus = gpu_names()
    dedicated = [g for g in gpus if not is_integrated(g)]
    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "is_64bit": sys.maxsize > 2**32,
        "cpu": cpu_name(),
        "cores": os.cpu_count() or 0,
        "ram_total_mb": total,
        "ram_available_mb": avail,
        "gpus": gpus,
        "gpu_main": (dedicated or gpus or ["Unknown GPU"])[0],
        "integrated_only": bool(gpus) and not dedicated,
    }


# -------------------------------------------------------- RAM guidance ---
HARD_FLOOR_MB = 1024


def reserve_for_os(total_mb):
    """Memory to leave for the OS, browser, Discord, etc."""
    if total_mb < 8192:
        return 2048
    if total_mb < 16384:
        return 3072
    return 4096


def ram_limits(total_mb):
    """(max_safe, hard_max). Above max_safe we warn; above hard_max we refuse,
    because the OS starts paging and the whole PC stutters."""
    if total_mb <= 0:
        return 8192, 16384  # unknown machine: be conservative but usable
    max_safe = max(HARD_FLOOR_MB, total_mb - reserve_for_os(total_mb))
    hard_max = max(HARD_FLOOR_MB, total_mb - 1024)
    return max_safe, hard_max


def recommend_ram(total_mb, kind="vanilla"):
    """kind: 'vanilla', 'modded' (performance/light mods) or 'modpack' (100+ mods)."""
    if total_mb and total_mb < 6144:
        base = 2048
    elif total_mb and total_mb < 12288:
        base = 3072
    else:
        base = 4096
    want = {"vanilla": base, "modded": base + 1024, "modpack": max(6144, base + 2048)}.get(kind, base)
    max_safe, _ = ram_limits(total_mb)
    return int(min(want, max_safe) // 512 * 512) or HARD_FLOOR_MB


def check_ram(alloc_mb, total_mb, kind="vanilla"):
    """Return a list of {'level': 'ok'|'warn'|'error', 'msg': str}."""
    out = []
    max_safe, hard_max = ram_limits(total_mb)
    rec = recommend_ram(total_mb, kind)
    if alloc_mb > hard_max:
        out.append({"level": "error", "msg": f"{alloc_mb/1024:.1f} GB is more than this PC can spare "
                    f"({total_mb/1024:.1f} GB total). Windows would start swapping and everything would stutter. "
                    f"The launcher will cap it at {hard_max/1024:.1f} GB."})
    elif alloc_mb > max_safe:
        out.append({"level": "warn", "msg": f"{alloc_mb/1024:.1f} GB leaves under {reserve_for_os(total_mb)/1024:.0f} GB "
                    f"for Windows and other apps. Try {rec/1024:.1f} GB."})
    elif alloc_mb > rec * 2 and alloc_mb > 6144:
        out.append({"level": "warn", "msg": f"{alloc_mb/1024:.1f} GB is far more than needed ({rec/1024:.1f} GB recommended). "
                    "Extra RAM does not raise FPS and can make garbage collection pauses longer."})
    elif alloc_mb < 2048:
        out.append({"level": "warn", "msg": "Under 2 GB is likely to cause lag spikes or out-of-memory crashes."})
    else:
        out.append({"level": "ok", "msg": f"{alloc_mb/1024:.1f} GB is a sensible amount (recommended: {rec/1024:.1f} GB)."})
    return out


def clamp_ram(alloc_mb, total_mb):
    _, hard_max = ram_limits(total_mb)
    return int(max(HARD_FLOOR_MB, min(alloc_mb, hard_max)))


# ------------------------------------------------------------- presets ---
# G1 settings commonly used for the Minecraft client to shorten GC pauses.
# They are widely used, but we have not benchmarked them here.
G1_LOW_PAUSE = ["-XX:+UseG1GC", "-XX:+UnlockExperimentalVMOptions", "-XX:G1NewSizePercent=20",
                "-XX:G1ReservePercent=20", "-XX:MaxGCPauseMillis=50", "-XX:G1HeapRegionSize=32M"]

PRESETS = {
    "low": {
        "name": "Low-End PC",
        "blurb": "Shorter view distance and fewer effects so weak or integrated GPUs stay playable. FPS capped at 60 to keep laptops cool.",
        "kind": "vanilla", "xms_full": False,
        "options": {"renderDistance": 6, "simulationDistance": 5, "particles": 2, "entityShadows": "false",
                    "biomeBlendRadius": 0, "enableVsync": "false", "maxFps": 60},
    },
    "balanced": {
        "name": "Balanced",
        "blurb": "Close to Minecraft's defaults with Java's default garbage collector. A safe starting point.",
        "kind": "vanilla", "xms_full": False,
        "options": {"renderDistance": 10, "simulationDistance": 8, "particles": 1, "entityShadows": "true",
                    "biomeBlendRadius": 2},
    },
    "maxfps": {
        "name": "Maximum FPS",
        "blurb": "Moderate view distance, no VSync, uncapped FPS, plus low-pause GC settings to reduce stutter.",
        "kind": "vanilla", "xms_full": True,
        "options": {"renderDistance": 8, "simulationDistance": 6, "particles": 1, "entityShadows": "false",
                    "biomeBlendRadius": 1, "enableVsync": "false", "maxFps": 260},
    },
    "high": {
        "name": "High-End PC",
        "blurb": "Long view distance and full effects. Uses ZGC on PCs with 8+ cores and 8+ GB for the game, which keeps GC pauses very short.",
        "kind": "vanilla", "xms_full": True,
        "options": {"renderDistance": 16, "simulationDistance": 10, "particles": 0, "entityShadows": "true",
                    "biomeBlendRadius": 3, "enableVsync": "false", "maxFps": 260},
    },
}


def suggest_preset(hw):
    total, cores = hw.get("ram_total_mb", 0), hw.get("cores", 0)
    if total and total < 8192 or hw.get("integrated_only"):
        return "low"
    if total >= 16384 and cores >= 8 and not hw.get("integrated_only"):
        return "high"
    return "balanced"


def jvm_args(preset, xmx_mb, xms_mb=None, java_major=None, cores=0, extra=""):
    """Build JVM args. Only adds flags that are valid for the given Java version."""
    xmx_mb = int(xmx_mb)
    p = PRESETS.get(preset, PRESETS["balanced"])
    if xms_mb is None:
        xms_mb = xmx_mb if p["xms_full"] else min(1024, xmx_mb)
    xms_mb = int(min(xms_mb, xmx_mb))
    args = [f"-Xmx{xmx_mb}M", f"-Xms{xms_mb}M"]
    if preset == "high" and cores >= 8 and xmx_mb >= 8192 and (java_major or 0) >= 21:
        args += ["-XX:+UseZGC"]  # generational by default on Java 23+
    elif preset in ("maxfps", "high", "low"):
        args += G1_LOW_PAUSE
    # User's own flags last so they win; drop heap flags that would contradict ours.
    for a in (extra or "").split():
        if not re.match(r"-Xm[sx]\d", a):
            args.append(a)
    return args
