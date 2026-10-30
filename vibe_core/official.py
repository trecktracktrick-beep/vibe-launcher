"""Hand-off to the official Minecraft Launcher.

Vibe prepares everything (version, mod loader, game folder, RAM/JVM flags) and
registers it as an "installation" in the official launcher, which then signs in
with the player's own Microsoft account. No login is bypassed.

The official launcher keeps installations in <.minecraft>/launcher_profiles.json
(the Microsoft Store / Xbox app edition uses launcher_profiles_microsoft_store.json).
We only add/update our own entries (keys starting with "vibe-") and keep
everything else exactly as it was.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROFILE_FILES = ("launcher_profiles.json", "launcher_profiles_microsoft_store.json")
KEY_PREFIX = "vibe-"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class LauncherNotSetUp(RuntimeError):
    pass


def profile_files(mc_dir):
    return [Path(mc_dir) / n for n in PROFILE_FILES if (Path(mc_dir) / n).exists()]


def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def upsert_installation(mc_dir, key, name, version_id, game_dir=None, java_args=(), backup_dir=None):
    """Create/update one installation in every official-launcher profile file.
    Returns the list of files written. Raises LauncherNotSetUp if the official
    launcher has never been opened (no profile file yet)."""
    if not key.startswith(KEY_PREFIX):
        key = KEY_PREFIX + key
    files = profile_files(mc_dir)
    if not files:
        raise LauncherNotSetUp("The official Minecraft Launcher hasn't been set up on this PC yet. "
                               "Open it once and sign in, then press Play here again.")
    written = []
    for f in files:
        raw = f.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if backup_dir:  # keep the untouched original once, forever
            orig = Path(backup_dir) / f"original-{f.name}"
            if not orig.exists():
                orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, orig)
        profiles = data.setdefault("profiles", {})
        entry = dict(profiles.get(key, {}))
        entry.update({
            "name": name,
            "type": "custom",
            "lastVersionId": version_id,
            "icon": entry.get("icon", "Crafting_Table"),
            "lastUsed": _iso_now(),
            "javaArgs": " ".join(java_args),
        })
        entry.setdefault("created", _iso_now())
        if game_dir:
            entry["gameDir"] = str(game_dir)
        else:
            entry.pop("gameDir", None)
        profiles[key] = entry
        f.with_suffix(f.suffix + ".vibe-bak").write_text(raw, encoding="utf-8")  # previous version
        tmp = f.with_suffix(f.suffix + ".vibe-tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, f)
        written.append(str(f))
    return written


def remove_installation(mc_dir, key):
    if not key.startswith(KEY_PREFIX):
        key = KEY_PREFIX + key
    for f in profile_files(mc_dir):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if key in data.get("profiles", {}):
            del data["profiles"][key]
            f.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------- open it ---
def _windows_candidates():
    env = os.environ.get
    return [
        Path(env("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Minecraft Launcher" / "MinecraftLauncher.exe",
        Path(env("PROGRAMFILES", r"C:\Program Files")) / "Minecraft Launcher" / "MinecraftLauncher.exe",
        Path(r"C:\XboxGames\Minecraft Launcher\Content\Minecraft.exe"),
        Path(env("LOCALAPPDATA", "")) / "Programs" / "Minecraft Launcher" / "MinecraftLauncher.exe",
    ]


def find_launcher(custom_path=""):
    """('exe', path) | ('app', AppUserModelID) | ('mac', path) | ('linux', cmd) | None"""
    if custom_path and Path(custom_path).exists():
        return ("exe", str(custom_path))
    if sys.platform == "win32":
        for c in _windows_candidates():
            if str(c) and c.exists():
                return ("exe", str(c))
        # Microsoft Store / Xbox app edition: ask Windows for its app id.
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-StartApps | Where-Object { $_.Name -like '*Minecraft Launcher*' } | "
                 "Select-Object -First 1 -ExpandProperty AppID"],
                capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW).stdout.strip()
            if out:
                return ("app", out)
        except Exception:
            pass
        return None
    if sys.platform == "darwin":
        p = Path("/Applications/Minecraft.app")
        return ("mac", str(p)) if p.exists() else None
    import shutil as sh
    for cmd in ("minecraft-launcher", "minecraft"):
        if sh.which(cmd):
            return ("linux", sh.which(cmd))
    return None


def open_launcher(found):
    kind, target = found
    if kind == "exe":
        subprocess.Popen([target], cwd=str(Path(target).parent), creationflags=_NO_WINDOW)
    elif kind == "app":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"])
    elif kind == "mac":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen([target])
