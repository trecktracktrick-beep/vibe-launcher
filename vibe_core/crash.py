"""Explain Minecraft crashes in plain language.

Matches well-known error signatures in the game log, crash report and JVM
error file. It reports likely causes; it never changes any configuration.
"""
import re
from pathlib import Path

# (id, regex, title, explanation, fix)
RULES = [
    ("oom", r"java\.lang\.OutOfMemoryError",
     "Minecraft ran out of memory",
     "The game needed more RAM than it was allowed to use.",
     "Raise the RAM allocation a little (see Performance), or remove heavy mods/resource packs."),
    ("heap_reserve", r"Could not reserve enough space for .*heap|Invalid maximum heap size|"
                     r"Initial heap size set to a larger value than the maximum",
     "Java couldn't get the RAM it was given",
     "The RAM setting is larger than this PC can provide right now, or the heap settings contradict each other.",
     "Lower the RAM allocation in Performance, close other heavy apps, and try again."),
    ("java_version", r"UnsupportedClassVersionError|has been compiled by a more recent version of the Java Runtime|"
                     r"requires (?:Java|java) \d+",
     "Wrong Java version",
     "Some code (the game or a mod) needs a newer Java than the one used.",
     "Use Repair on this version so the correct Java is reinstalled. If a mod demands newer Java, check it supports your Minecraft version."),
    ("gl_driver", r"Pixel format not accelerated|GLFW error 65542|WGL: The driver does not appear to support OpenGL|"
                  r"Failed to create (?:the )?(?:OpenGL|GL) context|OpenGL 3\.2 .*not supported|GLFW error 65543",
     "Graphics driver problem",
     "Minecraft couldn't start OpenGL. This is almost always an outdated or missing GPU driver, or the game running on the wrong GPU.",
     "Update your GPU driver from NVIDIA/AMD/Intel. On laptops, set Java (javaw.exe) to 'High performance' in Windows Graphics settings."),
    ("gpu_native", r"EXCEPTION_ACCESS_VIOLATION[\s\S]{0,4000}?(atio6axx|atig6pxx|ig\d+icd64|nvoglv64|amdxc64)\.dll",
     "Graphics driver crashed",
     "The crash happened inside the GPU driver, not in Minecraft itself.",
     "Update or clean-reinstall your GPU driver. If it keeps happening, try without shader packs."),
    ("native_crash", r"A fatal error has been detected by the Java Runtime Environment",
     "Java crashed (native error)",
     "The Java process itself crashed, usually from a driver, overlay, or a faulty native library.",
     "Update GPU drivers and close overlays (recording tools, RGB software). Open the crash report for the faulting module."),
    ("mod_deps", r"Incompatible mods? (?:found|set)|Mod resolution encountered an incompatible mod set|"
                 r"requires (?:any version|version [^ ]+) of|Missing or unsupported mandatory dependencies|"
                 r"which is missing!|ModResolutionException",
     "A mod is missing a dependency or is incompatible",
     "At least one mod needs another mod (or a different version of something) that isn't installed.",
     "Open Mods: missing dependencies are listed there. Install them, or disable the mod that needs them."),
    ("mixin", r"Mixin apply (?:for mod \S+ )?failed|MixinApplyError|InvalidInjectionException|"
              r"MixinTransformerError|Critical injection failure",
     "A mod doesn't work with this game version (or with another mod)",
     "A mod tried to patch game code that is different in this version, or two mods patch the same code.",
     "Update the named mod, or disable it. Mods built for an older Minecraft version often cause this."),
    ("duplicate", r"DuplicateModsFoundException|Found duplicate mods|Duplicate mods? found|duplicate mod ids?",
     "The same mod is installed twice",
     "Two files provide the same mod, usually an old and a new version.",
     "Open Mods and remove the older copy."),
    ("optifine_sodium", r"(?i)optifine[\s\S]{0,2000}sodium|sodium[\s\S]{0,2000}optifine",
     "OptiFine conflicts with Sodium",
     "OptiFine and Sodium both replace the renderer and can't run together.",
     "Remove OptiFine (Sodium is the faster option on modern versions)."),
    ("session", r"Invalid session|Failed to login|Bad login|401 Unauthorized",
     "Your sign-in expired",
     "Minecraft rejected the session token.",
     "Sign out and sign in again."),
    ("network", r"UnknownHostException|ConnectException: Connection refused|SocketTimeoutException",
     "Network problem",
     "A download or connection failed.",
     "Check your internet connection and try again."),
]

_MOD_IN_MIXIN = re.compile(r"(?:from mod|for mod)\s+([A-Za-z0-9_\-]+)")


def analyze(text: str):
    """Return findings: [{'id', 'title', 'explain', 'fix', 'evidence'}]."""
    findings, seen = [], set()
    if not text:
        return findings
    for rid, rx, title, explain, fix in RULES:
        m = re.search(rx, text)
        if not m or rid in seen:
            continue
        seen.add(rid)
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.start())
        evidence = text[line_start: line_end if line_end != -1 else len(text)].strip()[:300]
        f = {"id": rid, "title": title, "explain": explain, "fix": fix, "evidence": evidence}
        if rid == "mixin":
            mods = sorted(set(_MOD_IN_MIXIN.findall(text)))[:5]
            if mods:
                f["fix"] = f"Likely culprit: {', '.join(mods)}. " + fix
        findings.append(f)
    # A specific native GPU crash makes the generic native crash redundant.
    if any(f["id"] == "gpu_native" for f in findings):
        findings = [f for f in findings if f["id"] != "native_crash"]
    return findings


def newest_report(game_dir, since_ts):
    """Newest crash report or hs_err file created after launch, else None."""
    cands = []
    gd = Path(game_dir)
    for pattern in ("crash-reports/crash-*.txt", "hs_err_pid*.log"):
        for p in gd.glob(pattern):
            try:
                if p.stat().st_mtime >= since_ts - 2:
                    cands.append(p)
            except OSError:
                pass
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def summarize(exit_code, log_text, report_text=""):
    findings = analyze((report_text or "") + "\n" + (log_text or ""))
    if findings:
        headline = findings[0]["title"]
    elif exit_code:
        headline = f"Minecraft closed with an error (exit code {exit_code})"
        findings = [{"id": "unknown", "title": headline,
                     "explain": "No known error pattern was recognised.",
                     "fix": "Open the crash report or Console for details. Try Repair, or disable recently added mods.",
                     "evidence": ""}]
    else:
        headline = ""
    return {"headline": headline, "findings": findings}
