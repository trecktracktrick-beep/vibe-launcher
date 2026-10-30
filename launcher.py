#!/usr/bin/env python3
"""
Vibe Launcher 2.0 - a standalone Minecraft: Java Edition launcher.

Run:   "Vibe Launcher.exe" (built with build_exe.bat), "Start Vibe Launcher.bat",
       or `python3 launcher.py` (add --browser to use a browser window instead)
Needs: Python 3.10+, minecraft-launcher-lib>=7 and pywebview (installed automatically)

Architecture: a small local HTTP server (127.0.0.1 only) serves ui.html into
Vibe's own native window (pywebview / WebView2). Vibe installs versions, Java
and mod loaders, signs you in with Microsoft and starts the game itself.
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FROZEN = getattr(sys, "frozen", False)  # running as the built .exe
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))

if FROZEN and sys.stdout is None:  # the .exe has no console: keep its output in a log file
    try:
        _log_dir = Path.home() / ".vibelauncher"
        _log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(_log_dir / "launcher.log", "w", encoding="utf-8", buffering=1)
    except OSError:
        pass


def _pause_and_exit(code=1):
    # Keeps the console open when the launcher was double-clicked.
    try:
        input("\nPress Enter to close this window...")
    except (EOFError, RuntimeError):
        pass
    sys.exit(code)


def _ensure(module, requirement):
    """Import a dependency, installing it on first run (not needed in the .exe)."""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        if FROZEN:
            raise
    print(f"First run: installing {requirement} (one time only)...")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--user", requirement])
    if rc != 0:
        subprocess.call([sys.executable, "-m", "pip", "install", requirement])
    import site
    importlib.invalidate_caches()
    user_site = site.getusersitepackages()
    if user_site not in sys.path:
        sys.path.append(user_site)
    return importlib.import_module(module)


try:
    mll = _ensure("minecraft_launcher_lib", "minecraft-launcher-lib>=7")
except ImportError:
    print("\nCouldn't install minecraft-launcher-lib automatically.\n"
          "Open a terminal and run:  py -m pip install \"minecraft-launcher-lib>=7\"")
    _pause_and_exit()

from vibe_core import crash, hardware, mods, msauth, official, servers, util  # noqa: E402
from vibe_core.backups import Backups, read_options, write_options  # noqa: E402
from vibe_core.secure_store import AccountStore  # noqa: E402

APP_NAME = "Vibe Launcher"
APP_VERSION = "2.0"
PORT = 25599
# Only these pages may be opened in the system browser from the app.
EXTERNAL_URLS = ("https://www.microsoft.com/link", "https://microsoft.com/link", "https://microsoft.com/devicelogin",
                 "https://www.microsoft.com/devicelogin", "https://login.microsoft.com/device",
                 "https://aka.ms/mce-reviewappid", "https://portal.azure.com/")

DATA_DIR = Path.home() / ".vibelauncher"
SETTINGS_FILE = DATA_DIR / "settings.json"
INSTALLS_FILE = DATA_DIR / "installs.json"
MANIFEST_CACHE = DATA_DIR / "version_manifest.json"
MANIFEST_TTL = 30 * 60
LOADERS = ("vanilla", "fabric", "neoforge", "forge")
LIB_VERSION = getattr(mll, "__version__", "") or (mll.utils.get_library_version()
                                                   if hasattr(mll.utils, "get_library_version") else "?")

DEFAULT_SETTINGS = {
    "client_id": "",
    "minecraft_dir": mll.utils.get_minecraft_directory(),
    "ram_mb": 4096,
    "min_ram_mb": 0,          # 0 = decided by the preset
    "extra_jvm_args": "",
    "show_snapshots": False,
    "last_version": "",
    "preset": "balanced",
    "compact": False,
    "profiles": [],
    "active_profile": "default",
    "settings_version": 3,
    "launch_mode": "direct",        # "direct" = Vibe starts the game itself; "official" = hand off to Mojang's launcher
    "official_launcher_path": "",
    "servers": json.loads(json.dumps(servers.SUGGESTED)),
    "last_server": "",
}


# ---------------------------------------------------------------- storage ---
def load_json(path, default):
    try:
        return {**default, **json.loads(Path(path).read_text())}
    except Exception:
        return json.loads(json.dumps(default))


def save_json(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def migrate(settings):
    """v1.0 -> v1.1: create a Default profile from the old single-version setup."""
    if not settings.get("profiles"):
        settings["profiles"] = [{"id": "default", "name": "Default", "version": settings.get("last_version", ""),
                                 "loader": "vanilla", "loader_version": "", "game_dir": ""}]
        settings["active_profile"] = "default"
    if settings.get("settings_version", 1) < 3:
        # 2.0: Vibe is its own launcher now, so it starts the game itself by default.
        settings["launch_mode"] = "direct"
    settings["settings_version"] = 3
    return settings


# ------------------------------------------------------------------ state ---
class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = migrate(load_json(SETTINGS_FILE, DEFAULT_SETTINGS))
        save_json(SETTINGS_FILE, self.settings)
        self.store = AccountStore(DATA_DIR)
        self.account = self.store.load()
        self.backups = Backups(DATA_DIR / "backups")
        self.busy = False
        self.job = ""
        self.status = "Ready"
        self.progress = 0
        self.progress_max = 0
        self.log = []
        self.log_base = 0          # index of self.log[0] in the full log
        self.notices = []
        self.notice_seq = 0
        self.game_running = False
        self.device = None         # device-code sign-in in progress (public parts only)
        self.login_error = ""
        self.last_crash = None
        self.hw = None
        self._versions = None
        self._installed = (None, [])

    # -- logging (tokens are always redacted)
    def secrets(self):
        a = self.account or {}
        return [a.get("access_token", ""), a.get("refresh_token", "")]

    def push_log(self, line):
        line = util.redact(line.rstrip(), self.secrets())
        with self.lock:
            self.log.append(line)
            if len(self.log) > 1500:
                drop = len(self.log) - 1000
                self.log = self.log[drop:]
                self.log_base += drop

    def set_status(self, text):
        with self.lock:
            self.status = text
        self.push_log(f"[launcher] {text}")

    def notify(self, msg, level="info", **extra):
        with self.lock:
            self.notice_seq += 1
            self.notices.append({"id": self.notice_seq, "level": level, "msg": msg, **extra})
            self.notices = self.notices[-30:]

    def save_settings(self):
        with self.lock:
            save_json(SETTINGS_FILE, self.settings)


S = State()


def active_profile():
    ps = S.settings["profiles"]
    return next((p for p in ps if p["id"] == S.settings["active_profile"]), ps[0])


def profile_game_dir(p):
    return p.get("game_dir") or S.settings["minecraft_dir"]


# --------------------------------------------------------- hardware info ---
def hw():
    if S.hw is None:
        S.hw = hardware.detect()
    return S.hw


def refresh_ram():
    """RAM availability changes; CPU/GPU don't. Cheap to refresh."""
    info = hw()
    info["ram_total_mb"], info["ram_available_mb"] = hardware.ram_mb()
    return info


# --------------------------------------------------------------- versions ---
def version_manifest():
    """Version list with a 30-min disk cache; works offline from the cache."""
    if S._versions and time.time() - S._versions[0] < MANIFEST_TTL:
        return S._versions[1]
    cached = None
    try:
        cached = json.loads(MANIFEST_CACHE.read_text())
        if time.time() - cached["time"] < MANIFEST_TTL:
            S._versions = (cached["time"], cached["versions"])
            return cached["versions"]
    except Exception:
        pass
    try:
        vs = [{"id": v["id"], "type": v["type"]} for v in mll.utils.get_version_list()]
        S._versions = (time.time(), vs)
        save_json(MANIFEST_CACHE, {"time": time.time(), "versions": vs})
        return vs
    except Exception:
        if cached:
            S.push_log("[launcher] Offline: using cached version list")
            S._versions = (time.time(), cached["versions"])
            return cached["versions"]
        raise


def installed_versions(mc_dir):
    """Cached by the versions folder's modification time. v1.0 re-read every
    version JSON on every UI poll (several times a second)."""
    vdir = Path(mc_dir) / "versions"
    try:
        key = (str(vdir), vdir.stat().st_mtime_ns)
    except OSError:
        return []
    if S._installed[0] == key:
        return S._installed[1]
    try:
        ids = [v["id"] for v in mll.utils.get_installed_versions(mc_dir) if is_new_enough(v["id"])]
    except Exception:
        ids = []
    S._installed = (key, ids)
    return ids


MIN_MAJOR = 26  # only show Minecraft 26.x and newer


def is_new_enough(version_id):
    """True for '26.1', '26.2.1', '26.3-snapshot-2', and modded ids that embed them
    (e.g. 'fabric-loader-0.17-26.2'). False for old-style '1.21.x' and older."""
    import re
    for m in re.finditer(r"(?<![\d.])(\d+)\.\d+", version_id):
        if int(m.group(1)) >= MIN_MAJOR:
            return True
    return False


def version_json(mc_dir, vid):
    try:
        return json.loads((Path(mc_dir) / "versions" / vid / f"{vid}.json").read_text())
    except Exception:
        return None


def java_requirement(mc_dir, vid):
    """(major_version, runtime_component) from the version JSON (following inheritsFrom)."""
    seen = 0
    while vid and seen < 5:
        j = version_json(mc_dir, vid)
        if not j:
            return None, None
        if j.get("javaVersion"):
            return j["javaVersion"].get("majorVersion"), j["javaVersion"].get("component")
        vid = j.get("inheritsFrom")
        seen += 1
    return None, None


def install_complete(mc_dir, launch_id):
    """Fast path: skip the full file re-check when we finished this install
    before and the key files are still there. Repair forces a full check."""
    try:
        marks = json.loads(INSTALLS_FILE.read_text())
    except Exception:
        marks = {}
    if launch_id not in marks:
        return False
    j = version_json(mc_dir, launch_id)
    if not j:
        return False
    base = j.get("inheritsFrom") or launch_id
    if not version_json(mc_dir, base):
        return False
    if not (Path(mc_dir) / "versions" / base / f"{base}.jar").exists():
        return False
    _, comp = java_requirement(mc_dir, launch_id)
    if comp and not (Path(mc_dir) / "runtime" / comp).exists():
        return False
    return True


def mark_installed(launch_id):
    try:
        marks = json.loads(INSTALLS_FILE.read_text())
    except Exception:
        marks = {}
    marks[launch_id] = {"time": time.time(), "lib": LIB_VERSION}
    save_json(INSTALLS_FILE, marks)


# ------------------------------------------------------------------ auth ---
def refresh_account():
    acct = S.account
    if not acct or not acct.get("refresh_token"):
        return None
    try:
        fresh = msauth.refresh(S.settings["client_id"].strip(), acct["refresh_token"])
    except msauth.AuthError as e:
        S.push_log(f"[launcher] Session refresh failed: {e.code or e}")
        return None
    S.account = fresh
    S.store.save(fresh)
    return fresh


def device_login_worker(client_id, info):
    """Poll Microsoft until the user has entered the code, then finish the
    Xbox/Minecraft sign-in. Cancelling (or starting again) stops this loop."""
    my = info["device_code"]
    interval = max(2, int(info.get("interval") or 5))
    deadline = time.time() + int(info.get("expires_in") or 900)

    def still_mine():
        return S.device is not None and S.device.get("_id") == my
    try:
        while still_mine() and time.time() < deadline:
            time.sleep(interval)
            if not still_mine():
                return
            try:
                tok = msauth.poll_device_code(client_id, my)
            except msauth.Pending:
                continue
            except msauth.SlowDown:
                interval += 5
                continue
            with S.lock:
                if still_mine():
                    S.device["stage"] = "finishing"
            acct = msauth.minecraft_login(tok)
            if not still_mine():
                return
            S.account = acct
            S.store.save(acct)
            S.push_log(f"[launcher] Signed in as {acct['name']}")
            S.notify(f"Signed in as {acct['name']}", "success")
            with S.lock:
                S.device, S.login_error = None, ""
            return
        if still_mine():
            raise msauth.AuthError("The code expired. Start the sign-in again.", "expired")
    except msauth.AuthError as e:
        if still_mine():
            with S.lock:
                S.device, S.login_error = None, str(e)
            S.push_log(f"[launcher] Sign-in failed: {e.code}")
    except Exception as e:
        if still_mine():
            with S.lock:
                S.device, S.login_error = None, f"Sign-in failed ({type(e).__name__})."
            S.push_log(util.redact(traceback.format_exc(), S.secrets()))


def start_device_login():
    cid = S.settings["client_id"].strip()
    info = msauth.start_device_code(cid)
    with S.lock:
        S.login_error = ""
        S.device = {"_id": info["device_code"], "user_code": info["user_code"],
                    "verification_uri": info["verification_uri"] or "https://www.microsoft.com/link",
                    "expires_at": time.time() + int(info.get("expires_in") or 900), "stage": "waiting"}
    threading.Thread(target=device_login_worker, args=(cid, info), daemon=True).start()
    return {k: v for k, v in S.device.items() if not k.startswith("_")}


# -------------------------------------------------------------- launching ---
def _callbacks():
    def st(t):
        with S.lock:
            S.status = t

    def pr(p):
        with S.lock:
            S.progress = p

    def mx(m):
        with S.lock:
            S.progress_max = m
    return {"setStatus": st, "setProgress": pr, "setMax": mx}


def _with_retries(what, fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1:
                raise
            wait = 3 * (i + 1)
            S.set_status(f"{what} failed ({type(e).__name__}); retrying in {wait}s ({i + 2}/{attempts})...")
            time.sleep(wait)


def prepare_game(p, version, repair=False):
    """Install (or fast-path skip) the version + mod loader and work out JVM args.
    Shared by direct launch and the official-launcher hand-off."""
    mc_dir = S.settings["minecraft_dir"]
    loader = p.get("loader", "vanilla")
    launch_id = version
    ml = loader_ver = None
    if loader != "vanilla":
        ml = mll.mod_loader.get_mod_loader(loader)
        if not ml.is_minecraft_version_supported(version):
            raise RuntimeError(f"{ml.get_name()} doesn't support Minecraft {version} yet.")
        loader_ver = p.get("loader_version") or ml.get_latest_loader_version(version)
        launch_id = ml.get_installed_version(version, loader_ver)

    if repair or not install_complete(mc_dir, launch_id):
        S.set_status(f"{'Repairing' if repair else 'Installing'} {launch_id} (first time can take a few minutes)...")
        if ml:
            _with_retries("Install", lambda: ml.install(version, mc_dir, loader_version=loader_ver,
                                                         callback=_callbacks()))
        else:
            _with_retries("Install", lambda: mll.install.install_minecraft_version(version, mc_dir,
                                                                                   callback=_callbacks()))
        mark_installed(launch_id)
    else:
        S.push_log(f"[launcher] {launch_id} already installed; skipping full file check (use Repair if needed)")

    java_major, _ = java_requirement(mc_dir, launch_id)
    info = refresh_ram()
    ram = int(S.settings["ram_mb"])
    capped = hardware.clamp_ram(ram, info["ram_total_mb"])
    if capped != ram:
        S.notify(f"RAM capped at {capped/1024:.1f} GB to keep Windows stable.", "warn")
    jvm = hardware.jvm_args(S.settings["preset"], capped, S.settings["min_ram_mb"] or None,
                            java_major, info["cores"], S.settings["extra_jvm_args"])
    with S.lock:
        S.settings["last_version"] = version
        p["version"] = version
        if ml and loader_ver:
            p["loader_version"] = loader_ver
        S.progress = S.progress_max = 0
    S.save_settings()
    return launch_id, java_major, jvm


def record_crash(game_dir, launch_id, code, log_text, started):
    report = crash.newest_report(game_dir, started)
    report_text = ""
    if report:
        try:
            report_text = report.read_text(errors="replace")[:200_000]
        except OSError:
            pass
    summary = crash.summarize(code, util.redact(log_text, S.secrets()), report_text)
    if not summary["findings"]:
        return False
    latest = Path(game_dir) / "logs" / "latest.log"
    S.last_crash = {"time": time.time(), "exit_code": code, "version": launch_id, **summary,
                    "report_path": str(report) if report else "",
                    "log_path": str(latest) if latest.exists() else "",
                    "seconds": round(time.time() - started)}
    S.set_status(f"Crashed: {summary['headline']}")
    S.notify(summary["headline"], "error", crash=True)
    return True


SERVERS_DAT_BACKUPS = DATA_DIR / "backups" / "servers-dat"


def find_server(sid):
    srv = next((s for s in S.settings.get("servers", []) if s.get("id") == sid), None)
    if not srv:
        raise ValueError("Unknown server")
    return srv


def server_address(srv):
    host, port = servers.parse_address(srv["address"])
    return servers.format_address(host, port)


def remember_server(game_dir, srv):
    """Put the server at the top of this profile's in-game Multiplayer list.
    The old servers.dat is backed up first (last 10 kept). Never blocks joining."""
    f = Path(game_dir) / "servers.dat"
    try:
        old = f.read_bytes() if f.exists() else None
        if servers.add_to_server_list(game_dir, srv["name"], server_address(srv)) and old:
            SERVERS_DAT_BACKUPS.mkdir(parents=True, exist_ok=True)
            (SERVERS_DAT_BACKUPS / f"{time.strftime('%Y%m%d-%H%M%S')}-servers.dat").write_bytes(old)
            for stale in sorted(SERVERS_DAT_BACKUPS.glob("*-servers.dat"))[:-10]:
                stale.unlink()
        S.push_log(f"[launcher] '{srv['name']}' is at the top of the Multiplayer list")
        return True
    except Exception as e:
        S.push_log(f"[launcher] Couldn't update servers.dat: {e}")
        return False


_ping_cache = {}


def ping_server(address):
    hit = _ping_cache.get(address)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    r = servers.ping(address)
    _ping_cache[address] = (time.time(), r)
    return r


def save_server(b):
    name = str(b.get("name") or "").strip()[:40]
    host, port = servers.parse_address(b.get("address"))
    address = servers.format_address(host, port)
    with S.lock:
        lst = S.settings.setdefault("servers", [])
        existing = next((s for s in lst if s.get("id") == b.get("id")), None)
        if existing:
            existing.update(name=name or existing["name"], address=address)
            srv = existing
        else:
            if any(s["address"].lower() == address.lower() for s in lst):
                raise ValueError("That server is already in your list.")
            base = util.slugify(name or host)
            sid, i = base, 2
            while any(s["id"] == sid for s in lst):
                sid, i = f"{base}-{i}", i + 1
            srv = {"id": sid, "name": name or host, "address": address, "tags": [], "custom": True}
            lst.insert(0, srv)
    S.save_settings()
    return srv


def handoff_worker(version, repair, server=None):
    """Prepare everything, register it in the official launcher, open it."""
    p = active_profile()
    mc_dir = S.settings["minecraft_dir"]
    game_dir = profile_game_dir(p)
    try:
        Path(mc_dir).mkdir(parents=True, exist_ok=True)
        Path(game_dir).mkdir(parents=True, exist_ok=True)
        launch_id, java_major, jvm = prepare_game(p, version, repair)
        listed = remember_server(game_dir, server) if server else False
        then = (f" Then open Multiplayer: {server['name']} is at the top of the list."
                if listed else f" Then open Multiplayer and add {server['address']}." if server else "")
        name = f"Vibe · {p['name']}"
        files = official.upsert_installation(mc_dir, p["id"], name, launch_id,
                                             game_dir=p.get("game_dir") or None, java_args=jvm,
                                             backup_dir=DATA_DIR / "backups" / "official-launcher")
        S.push_log(f"[launcher] Registered '{name}' ({launch_id}) in: {', '.join(files)}")
        S.push_log(f"[launcher] Java {java_major or '?'} | JVM: {' '.join(jvm)}")
        found = official.find_launcher(S.settings.get("official_launcher_path", ""))
        if found:
            official.open_launcher(found)
            S.set_status(f"Opened the Minecraft Launcher. Pick '{name}' and press Play.")
            S.notify(f"In the Minecraft Launcher, pick the '{name}' installation and press Play.{then} "
                     "If the launcher was already open, close and reopen it so the installation shows up.",
                     "success")
        else:
            S.set_status(f"'{name}' is ready. Open the Minecraft Launcher and press Play.")
            S.notify("Couldn't find the Minecraft Launcher to open it. Open it yourself, pick "
                     f"'{name}' and press Play.{then} (You can set its path in Settings.)", "warn")
        threading.Thread(target=watch_for_crash, args=(game_dir, launch_id, time.time()), daemon=True).start()
    except official.LauncherNotSetUp as e:
        S.set_status("Official Minecraft Launcher not set up yet")
        S.notify(str(e), "error")
    except Exception as e:
        S.push_log(util.redact(traceback.format_exc(), S.secrets()))
        S.set_status(f"Error: {e}")
        S.notify(str(e), "error")
    finally:
        with S.lock:
            S.busy, S.job, S.progress, S.progress_max = False, "", 0, 0


def watch_for_crash(game_dir, launch_id, since, hours=6):
    """The official launcher runs the game, so we can't see its exit code. Instead
    we watch for a new crash report in this profile's folder for a few hours."""
    my_id = since
    S._crash_watch = my_id
    deadline = since + hours * 3600
    while time.time() < deadline and getattr(S, "_crash_watch", None) == my_id:
        time.sleep(10)
        report = crash.newest_report(game_dir, since)
        if report:
            log = ""
            try:
                log = (Path(game_dir) / "logs" / "latest.log").read_text(errors="replace")[-200_000:]
            except OSError:
                pass
            record_crash(game_dir, launch_id, 1, log, since)
            return


def play_worker(version, demo, repair, server=None):
    p = active_profile()
    mc_dir = S.settings["minecraft_dir"]
    game_dir = profile_game_dir(p)
    try:
        Path(mc_dir).mkdir(parents=True, exist_ok=True)
        Path(game_dir).mkdir(parents=True, exist_ok=True)

        if demo:
            options = {"username": "Player", "uuid": str(uuid.uuid4()), "token": "", "demo": True}
        else:
            S.set_status("Refreshing your session...")
            acct = refresh_account()
            if not acct:
                with S.lock:
                    S.account = None
                S.set_status("Sign-in expired. Please sign in again.")
                S.notify("Your sign-in expired. Please sign in again.", "error")
                return
            options = {"username": acct["name"], "uuid": acct["id"], "token": acct["access_token"]}

        launch_id, java_major, jvm = prepare_game(p, version, repair)
        options.update({"jvmArguments": jvm, "gameDirectory": game_dir,
                        "launcherName": APP_NAME.replace(" ", ""), "launcherVersion": APP_VERSION})
        if server:
            remember_server(game_dir, server)
            options["quickPlayMultiplayer"] = server_address(server)  # game connects straight away
        cmd = mll.command.get_minecraft_command(launch_id, mc_dir, options)
        if server and "--quickPlayMultiplayer" not in cmd:
            cmd += ["--quickPlayMultiplayer", server_address(server)]

        S.push_log(f"[launcher] Java {java_major or '?'} | JVM: {' '.join(jvm)}")
        S.push_log(f"[launcher] Game folder: {game_dir}")
        S.set_status(f"Launching {launch_id}" + (f" and joining {server['name']}..." if server else "..."))
        started = time.time()
        proc = subprocess.Popen(cmd, cwd=game_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
        with S.lock:
            S.game_running, S.busy, S.last_crash = True, False, None
        S.set_status(f"Playing {launch_id}" + (f" on {server['name']}" if server else ""))
        tail = []
        for line in proc.stdout:
            S.push_log(line)
            tail.append(line)
            if len(tail) > 800:
                del tail[:200]
        code = proc.wait()
        if code != 0 and record_crash(game_dir, launch_id, code, "".join(tail), started):
            pass
        else:
            S.set_status("Game closed")
    except Exception as e:
        S.push_log(util.redact(traceback.format_exc(), S.secrets()))
        S.set_status(f"Error: {e}")
        S.notify(str(e), "error")
    finally:
        with S.lock:
            S.busy = False
            S.game_running = False
            S.job = ""
            S.progress = S.progress_max = 0


# ------------------------------------------------------------ optimizer ---
def mods_dir_for(p):
    return Path(profile_game_dir(p)) / "mods"


def optimizer_report():
    info = refresh_ram()
    p = active_profile()
    total = info["ram_total_mb"]
    mod_list = mods.list_mods(mods_dir_for(p)) if p["loader"] != "vanilla" else []
    kind = "vanilla" if p["loader"] == "vanilla" else ("modpack" if len(mod_list) > 60 else "modded")
    ram = int(S.settings["ram_mb"])
    checks = [dict(c, area="RAM") for c in hardware.check_ram(ram, total, kind)]

    if not info["is_64bit"]:
        checks.append({"area": "Java", "level": "error",
                       "msg": "This Python is 32-bit, so Windows is probably 32-bit too. Minecraft needs a 64-bit OS."})
    vid = p.get("version") or S.settings["last_version"]
    if vid:
        mc_dir = S.settings["minecraft_dir"]
        major, comp = java_requirement(mc_dir, vid)
        if major:
            ok = comp and (Path(mc_dir) / "runtime" / comp).exists()
            checks.append({"area": "Java", "level": "ok" if ok else "warn",
                           "msg": f"Minecraft {vid} needs Java {major}. "
                                  + ("Mojang's bundled runtime is installed." if ok else
                                     "It will be downloaded automatically on next launch.")})
        else:
            checks.append({"area": "Java", "level": "ok",
                           "msg": f"The right Java for {vid} is downloaded automatically on first launch."})
    if info["integrated_only"]:
        checks.append({"area": "GPU", "level": "warn",
                       "msg": f"Only integrated graphics found ({info['gpu_main']}). Keep render distance at 6-8 "
                              "and use the Low-End preset; Sodium helps a lot here."})
    elif len(info["gpus"]) > 1:
        checks.append({"area": "GPU", "level": "warn",
                       "msg": "More than one GPU found. On laptops, set javaw.exe to 'High performance' in "
                              "Windows Settings > System > Display > Graphics so Minecraft uses the dedicated GPU."})
    if any(a.startswith(("-Xmx", "-Xms")) for a in S.settings["extra_jvm_args"].split()):
        checks.append({"area": "JVM", "level": "warn",
                       "msg": "Your extra JVM arguments set -Xmx/-Xms. They're ignored; use the RAM slider instead."})
    if p["loader"] == "vanilla":
        checks.append({"area": "Mods", "level": "tip",
                       "msg": "The biggest FPS gain on most PCs comes from Sodium. Create a Fabric profile in "
                              "Profiles, then install the performance mods from Mods."})
    else:
        for pr in mods.find_problems(mod_list, p["loader"]):
            checks.append({"area": "Mods", "level": pr["level"], "msg": pr["msg"]})
        have = {m["id"] for m in mod_list if m["enabled"]}
        missing = [m["name"] for m in mods.PERFORMANCE_MODS
                   if m["slug"].replace("-", "") not in {h.replace("-", "") for h in have}]
        if missing:
            checks.append({"area": "Mods", "level": "tip",
                           "msg": f"Not installed yet: {', '.join(missing)}. Check compatibility in Mods."})
    opts = read_options(Path(profile_game_dir(p)) / "options.txt")
    try:
        rd = int(opts.get("renderDistance", "0"))
        if info["integrated_only"] and rd > 10:
            checks.append({"area": "Game", "level": "warn",
                           "msg": f"Render distance is {rd} chunks. On integrated graphics, 8 or less is much smoother."})
    except ValueError:
        pass

    presets = {}
    for key, pr in hardware.PRESETS.items():
        r = hardware.recommend_ram(total, kind)
        presets[key] = {**pr, "ram_mb": r,
                        "jvm": hardware.jvm_args(key, r, None, 21, info["cores"])}
    return {
        "hardware": info, "kind": kind, "profile": p,
        "ram": {"current": ram, "recommended": hardware.recommend_ram(total, kind),
                "max_safe": hardware.ram_limits(total)[0], "hard_max": hardware.ram_limits(total)[1]},
        "checks": checks, "presets": presets, "preset": S.settings["preset"],
        "suggested": hardware.suggest_preset(info),
        "game_options": {k: opts.get(k) for k in ("renderDistance", "simulationDistance", "maxFps",
                                                  "enableVsync", "particles", "entityShadows")},
        "backups": S.backups.list("optimizer")[:5],
        "token_storage": S.store.method(),
    }


PERF_KEYS = ("preset", "ram_mb", "min_ram_mb", "extra_jvm_args")


def optimizer_apply(preset, ram_mb, apply_options):
    if preset not in hardware.PRESETS:
        raise ValueError("Unknown preset")
    p = active_profile()
    opt_file = Path(profile_game_dir(p)) / "options.txt"
    bid = S.backups.create("optimizer", {k: S.settings[k] for k in PERF_KEYS},
                           files=[opt_file] if apply_options else [])
    total = refresh_ram()["ram_total_mb"]
    with S.lock:
        S.settings["preset"] = preset
        S.settings["ram_mb"] = hardware.clamp_ram(int(ram_mb), total)
        S.settings["min_ram_mb"] = 0
    S.save_settings()
    if apply_options:
        write_options(opt_file, hardware.PRESETS[preset]["options"])
    S.push_log(f"[launcher] Applied {hardware.PRESETS[preset]['name']} preset (backup {bid})")
    return bid


def optimizer_restore(bid=None):
    lst = S.backups.list("optimizer")
    if not lst:
        raise ValueError("No optimizer backup to restore")
    bid = bid or lst[0]["id"]
    saved = S.backups.restore(bid)
    with S.lock:
        for k in PERF_KEYS:
            if k in saved:
                S.settings[k] = saved[k]
    S.save_settings()
    S.push_log(f"[launcher] Restored settings from backup {bid}")
    return bid


# ------------------------------------------------------------------ mods ---
def mod_job(slugs):
    p = active_profile()
    loader, mc = p["loader"], p.get("version") or S.settings["last_version"]
    mdir = mods_dir_for(p)
    try:
        S.set_status("Checking compatibility and dependencies...")
        current = mods.list_mods(mdir)
        have = {m["id"] for m in current} | {m["project"] for m in current if m["project"]}
        try:
            src = json.loads((mdir / ".vibe-sources.json").read_text())
            have |= {v["project_id"] for v in src.values()}
        except Exception:
            pass
        plan = mods.plan_install(slugs, mc, loader, have)
        for u in plan["unavailable"]:
            why = f" (needed by {u['required_by']})" if u["required_by"] else ""
            S.notify(f"{u['project']}{why} has no version for {loader} {mc}. Skipped.", "warn")
        if not plan["files"]:
            S.set_status("Nothing to install")
            return
        if (Path(profile_game_dir(p)) / "config").is_dir():
            S.backups.create("mods-config", {}, zip_dirs=[Path(profile_game_dir(p)) / "config"])
        with S.lock:
            S.progress, S.progress_max = 0, len(plan["files"])

        def prog(done, total, name):
            with S.lock:
                S.progress, S.status = done, f"Downloading {name} ({done}/{total})"
        res = mods.install_plan(plan, mdir, prog)
        failed = [r for r in res if r["status"] == "failed"]
        ok = [r for r in res if r["status"] != "failed"]
        for r in failed:
            S.push_log(f"[mods] Failed: {r['file']}: {r['error']}")
        S.set_status(f"Mods: {len(ok)} installed" + (f", {len(failed)} failed" if failed else ""))
        deps = [f["filename"] for f in plan["files"] if f["required_by"]]
        S.notify(f"Installed {len(ok)} file(s)" + (f" incl. dependencies: {', '.join(deps)}" if deps else "")
                 + (f". {len(failed)} failed (see Console)." if failed else "."), "error" if failed else "success")
    except Exception as e:
        S.push_log(traceback.format_exc())
        S.set_status(f"Mod install error: {e}")
        S.notify(f"Mod install failed: {e}", "error")
    finally:
        with S.lock:
            S.busy, S.job, S.progress, S.progress_max = False, "", 0, 0


# ------------------------------------------------------------------- misc ---
def open_path(path):
    """Open a crash report/log/folder, only inside launcher-managed folders."""
    roots = [S.settings["minecraft_dir"], str(DATA_DIR)] + [profile_game_dir(p) for p in S.settings["profiles"]]
    if not any(util.is_within(path, r) for r in roots) or not Path(path).exists():
        raise ValueError("That path isn't one the launcher manages.")
    if sys.platform == "win32":
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def state_snapshot(log_from, notice_from):
    with S.lock:
        acct = None
        if S.account and S.account.get("name"):
            acct = {"name": S.account["name"], "id": S.account.get("id", "")}
        start = max(0, log_from - S.log_base)
        p = active_profile()
        return {
            "account": acct, "settings": S.settings, "profile": p,
            "busy": S.busy, "job": S.job, "status": S.status,
            "progress": S.progress, "progress_max": S.progress_max,
            "game_running": S.game_running, "login_error": S.login_error,
            "log": S.log[start:], "log_next": S.log_base + len(S.log),
            "log_reset": log_from < S.log_base,
            "notices": [n for n in S.notices if n["id"] > notice_from],
            "installed": installed_versions(S.settings["minecraft_dir"]),
            "device_login": ({k: v for k, v in S.device.items() if not k.startswith("_")} if S.device else None),
            "crash": S.last_crash,
            "app_version": APP_VERSION, "native": WINDOW is not None,
            "official_ready": bool(official.profile_files(S.settings["minecraft_dir"])),
        }


def start_job(kind, target, *args):
    with S.lock:
        if S.busy or S.game_running:
            raise RuntimeError("Busy: wait for the current task (or close the game) first.")
        S.busy, S.job = True, kind
    threading.Thread(target=target, args=args, daemon=True).start()


# ------------------------------------------------------------------- http ---
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _local_only(self):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        return (origin in ("", f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}")
                and host in (f"localhost:{PORT}", f"127.0.0.1:{PORT}"))

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self._send(200, (HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/favicon.ico":
                ico = HERE / "assets" / "vibe.ico"
                if ico.exists():
                    return self._send(200, ico.read_bytes(), "image/x-icon")
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._local_only():
                return self._json({"error": "forbidden"}, 403)
            if url.path == "/api/state":
                return self._json(state_snapshot(int(q.get("log", 0)), int(q.get("n", 0))))
            if url.path == "/api/versions":
                vs = [v for v in version_manifest() if is_new_enough(v["id"]) and
                      (v["type"] == "release" or (S.settings["show_snapshots"] and v["type"] == "snapshot"))]
                return self._json({"versions": vs})
            if url.path == "/api/optimizer":
                return self._json(optimizer_report())
            if url.path == "/api/mods":
                p = active_profile()
                if p["loader"] == "vanilla":
                    return self._json({"profile": p, "mods": [], "problems": [], "vanilla": True})
                ml = mods.list_mods(mods_dir_for(p))
                return self._json({"profile": p, "mods": ml, "problems": mods.find_problems(ml, p["loader"]),
                                   "mods_dir": str(mods_dir_for(p))})
            if url.path == "/api/mods/perf":
                p = active_profile()
                comp = mods.check_compat([m["slug"] for m in mods.PERFORMANCE_MODS], p["version"], p["loader"])
                return self._json({"mods": [{**m, **comp.get(m["slug"], {})} for m in mods.PERFORMANCE_MODS]})
            if url.path == "/api/mods/search":
                p = active_profile()
                return self._json({"results": mods.search(q.get("q", ""), p["version"], p["loader"])})
            if url.path == "/api/servers":
                return self._json({"servers": S.settings.get("servers", []),
                                   "last": S.settings.get("last_server", "")})
            if url.path == "/api/servers/ping":
                return self._json(ping_server(q.get("address", "")))
            if url.path == "/api/loader_check":
                ld, ver = q.get("loader", ""), q.get("version", "")
                if ld == "vanilla":
                    return self._json({"ok": True})
                ml = mll.mod_loader.get_mod_loader(ld)
                return self._json({"ok": ml.is_minecraft_version_supported(ver), "name": ml.get_name()})
            self._send(404, "not found", "text/plain")
        except Exception as e:
            S.push_log(util.redact(traceback.format_exc(), S.secrets()))
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        if not self._local_only():
            return self._json({"error": "forbidden"}, 403)
        path = urlparse(self.path).path
        try:
            body = self._body()
            r = self._post(path, body)
            return self._json(r if r is not None else {"ok": True})
        except (ValueError, RuntimeError, FileNotFoundError, msauth.AuthError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            S.push_log(util.redact(traceback.format_exc(), S.secrets()))
            return self._json({"error": str(e)}, 500)

    def _post(self, path, b):
        if path == "/api/settings":
            total = refresh_ram()["ram_total_mb"]
            with S.lock:
                for k in ("client_id", "minecraft_dir", "ram_mb", "min_ram_mb", "extra_jvm_args",
                          "show_snapshots", "compact", "preset", "launch_mode", "official_launcher_path"):
                    if k in b:
                        S.settings[k] = b[k]
                S.settings["ram_mb"] = hardware.clamp_ram(int(S.settings["ram_mb"]), total)
                S.settings["min_ram_mb"] = max(0, min(int(S.settings["min_ram_mb"] or 0), S.settings["ram_mb"]))
                if S.settings["launch_mode"] not in ("official", "direct"):
                    S.settings["launch_mode"] = "official"
                if S.settings["preset"] not in hardware.PRESETS:
                    S.settings["preset"] = "balanced"
            S.save_settings()
            return {"ok": True, "ram_mb": S.settings["ram_mb"]}
        if path == "/api/ram_check":
            info = refresh_ram()
            p = active_profile()
            kind = "vanilla" if p["loader"] == "vanilla" else "modded"
            return {"checks": hardware.check_ram(int(b.get("ram_mb", 0)), info["ram_total_mb"], kind),
                    "recommended": hardware.recommend_ram(info["ram_total_mb"], kind),
                    "hard_max": hardware.ram_limits(info["ram_total_mb"])[1], "total": info["ram_total_mb"]}
        if path == "/api/logout":
            S.account = None
            S.store.clear()
            return None
        if path == "/api/login/start":
            return {"ok": True, "device": start_device_login()}
        if path == "/api/login/cancel":
            with S.lock:
                S.device = None
            return None
        if path == "/api/open_url":
            url = str(b.get("url", ""))
            if not url.startswith(EXTERNAL_URLS):
                raise ValueError("That link can't be opened from the launcher.")
            webbrowser.open(url)
            return None
        if path == "/api/window":
            window_action(b.get("action", ""))
            return None
        if path == "/api/focus":
            window_action("focus")
            return None
        if path == "/api/quit":
            quit_app()
            return None
        if path == "/api/play":
            version = str(b.get("version", "")).strip()
            demo = bool(b.get("demo"))
            if not version:
                raise ValueError("Pick a version")
            if not is_new_enough(version):
                raise ValueError("Only Minecraft 26.x and newer are supported.")
            if not demo and S.settings.get("launch_mode") == "official":
                start_job("play", handoff_worker, version, bool(b.get("repair")))
                return None
            if not demo and not S.account:
                raise ValueError("Sign in first (or use Demo mode)")
            start_job("play", play_worker, version, demo, bool(b.get("repair")))
            return None
        if path == "/api/servers/save":
            return {"ok": True, "server": save_server(b)}
        if path == "/api/servers/remove":
            with S.lock:
                S.settings["servers"] = [s for s in S.settings.get("servers", []) if s.get("id") != b.get("id")]
            S.save_settings()
            return None
        if path == "/api/servers/restore":
            with S.lock:
                lst = S.settings.setdefault("servers", [])
                have = {s["address"].lower() for s in lst}
                lst += [dict(s) for s in servers.SUGGESTED if s["address"].lower() not in have]
            S.save_settings()
            return None
        if path == "/api/servers/join":
            srv = find_server(b.get("id"))
            server_address(srv)  # validates before anything starts
            version = str(b.get("version", "")).strip()
            if not version:
                raise ValueError("Pick a version")
            if not is_new_enough(version):
                raise ValueError("Only Minecraft 26.x and newer are supported.")
            with S.lock:
                S.settings["last_server"] = srv["id"]
            S.save_settings()
            if S.settings.get("launch_mode") == "official":
                start_job("play", handoff_worker, version, False, srv)
                return None
            if not S.account:
                raise ValueError("Sign in first. (The free demo can't join servers.)")
            start_job("play", play_worker, version, False, False, srv)
            return None
        if path == "/api/optimizer/apply":
            bid = optimizer_apply(b.get("preset", "balanced"), b.get("ram_mb") or S.settings["ram_mb"],
                                  bool(b.get("apply_options")))
            return {"ok": True, "backup": bid}
        if path == "/api/optimizer/restore":
            return {"ok": True, "backup": optimizer_restore(b.get("id"))}
        if path == "/api/profiles/save":
            return {"ok": True, "profile": save_profile(b)}
        if path == "/api/profiles/select":
            if not any(p["id"] == b.get("id") for p in S.settings["profiles"]):
                raise ValueError("Unknown profile")
            with S.lock:
                S.settings["active_profile"] = b["id"]
            S.save_settings()
            return None
        if path == "/api/profiles/delete":
            pid = b.get("id")
            if pid == "default":
                raise ValueError("The Default profile can't be deleted.")
            try:
                official.remove_installation(S.settings["minecraft_dir"], pid)
            except Exception:
                pass
            with S.lock:
                S.settings["profiles"] = [p for p in S.settings["profiles"] if p["id"] != pid]
                if S.settings["active_profile"] == pid:
                    S.settings["active_profile"] = "default"
            S.save_settings()
            return {"ok": True, "note": "Profile removed. Its game folder (worlds, mods) was kept on disk."}
        if path == "/api/mods/install":
            slugs = [str(s) for s in b.get("slugs", [])][:20]
            p = active_profile()
            if p["loader"] == "vanilla":
                raise ValueError("Mods need a Fabric, NeoForge or Forge profile.")
            if not p.get("version"):
                raise ValueError("Pick a Minecraft version for this profile first.")
            start_job("mods", mod_job, slugs)
            return None
        if path == "/api/mods/toggle":
            mods.set_enabled(mods_dir_for(active_profile()), b.get("file", ""), bool(b.get("enabled")))
            return None
        if path == "/api/mods/remove":
            moved = mods.remove_mod(mods_dir_for(active_profile()), b.get("file", ""), DATA_DIR / "trash")
            return {"ok": True, "moved_to": moved}
        if path == "/api/open":
            target = b.get("path", "")
            if b.get("what") == "mods":
                target = str(mods_dir_for(active_profile()))
                Path(target).mkdir(parents=True, exist_ok=True)
            elif b.get("what") == "game":
                target = profile_game_dir(active_profile())
            open_path(target)
            return None
        if path == "/api/crash/dismiss":
            S.last_crash = None
            return None
        raise ValueError("not found")


def save_profile(b):
    name = (str(b.get("name") or "").strip() or "Profile")[:40]
    loader = b.get("loader", "vanilla")
    if loader not in LOADERS:
        raise ValueError("Unknown mod loader")
    version = str(b.get("version") or "").strip()
    if version and not is_new_enough(version):
        raise ValueError("Only Minecraft 26.x and newer are supported.")
    with S.lock:
        ps = S.settings["profiles"]
        existing = next((p for p in ps if p["id"] == b.get("id")), None)
        if existing:
            if existing["id"] == "default" and loader != "vanilla":
                raise ValueError("Keep Default vanilla; create a new profile for mods (it gets its own folder).")
            if existing.get("loader") != loader:
                existing["loader_version"] = ""
            existing.update(name=name, loader=loader, version=version or existing.get("version", ""))
            prof = existing
        else:
            base = util.slugify(name)
            pid, i = base, 2
            while any(p["id"] == pid for p in ps):
                pid, i = f"{base}-{i}", i + 1
            # Modded profiles get their own folder so mods never touch your main worlds.
            gdir = "" if loader == "vanilla" else str(Path(S.settings["minecraft_dir"]) / "vibe-profiles" / pid)
            prof = {"id": pid, "name": name, "version": version, "loader": loader, "loader_version": "",
                    "game_dir": gdir}
            ps.append(prof)
        S.settings["active_profile"] = prof["id"]
    S.save_settings()
    return prof


class LocalServer(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a second program bind the same port. Take it
    exclusively instead, so a second Vibe (or anything else) can't share it."""
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self):
        import socket
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ------------------------------------------------------------ app window ---
SERVER = None
WINDOW = None   # Vibe's own native window (pywebview), when available


def window_action(action):
    """Title-bar buttons in the UI call this (the window has no system frame)."""
    w = WINDOW
    if not w:
        return
    if action == "minimize":
        w.minimize()
    elif action == "maximize":
        if getattr(S, "_maximized", False):
            w.restore()
        else:
            w.maximize()
    elif action == "focus":
        w.restore()
        w.show()
        try:  # bring it to the front on Windows
            w.on_top = True
            w.on_top = False
        except Exception:
            pass
    elif action == "close":
        w.destroy()


def quit_app():
    def go():
        time.sleep(0.4)  # let the UI show its goodbye screen
        if WINDOW:
            try:
                WINDOW.destroy()
            except Exception:
                pass
        SERVER.shutdown()
    threading.Thread(target=go, daemon=True).start()


def find_app_browser():
    """Find a Chromium-based browser that can open the launcher as its own app window."""
    import shutil
    if sys.platform == "win32":
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        cands = [os.path.join(r, p) for p in rels for r in roots if r]
    elif sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    else:
        cands = [shutil.which(n) or "" for n in
                 ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def open_app_window(url):
    exe = find_app_browser()
    if exe:
        size = "760,620" if S.settings.get("compact") else "1180,740"
        try:
            return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={DATA_DIR / 'window'}",
                                     f"--window-size={size}", "--no-first-run", "--no-default-browser-check",
                                     "--disable-features=Translate"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    webbrowser.open(url)
    return None


def console_window(show):
    """Hide the console once Vibe's own window is up (logs are in the Console panel)."""
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 5 if show else 0)  # SW_SHOW / SW_HIDE
        except Exception:
            pass


def watch_window(proc):
    """Browser fallback: quit when its window is closed (after the game/job ends)."""
    started = time.time()
    proc.wait()
    if time.time() - started < 8:
        # The browser handed the window to an already-running copy of itself, so we
        # can't tell when it closes. Keep running; Quit in the launcher stops it.
        return
    while S.game_running or S.busy:
        time.sleep(2)
    print("Launcher window closed.")
    SERVER.shutdown()


def load_webview():
    if "--browser" in sys.argv:
        return None
    try:
        return _ensure("webview", "pywebview>=5")
    except Exception as e:
        print(f"Native window not available ({type(e).__name__}: {e}); using a browser window instead.")
        return None


def run_native(webview, url):
    """Vibe's own frameless window. Blocks until it's closed."""
    global WINDOW
    if sys.platform == "win32":
        try:  # its own taskbar entry and icon, not Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Vibe.Launcher")
        except Exception:
            pass
    compact = S.settings.get("compact")
    WINDOW = webview.create_window(
        APP_NAME, url, width=760 if compact else 1180, height=620 if compact else 740,
        min_size=(720, 560), frameless=True, easy_drag=False, background_color="#0b0c0f", text_select=False)
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:
        pass
    WINDOW.events.shown += lambda: console_window(False)
    WINDOW.events.maximized += lambda: setattr(S, "_maximized", True)
    WINDOW.events.restored += lambda: setattr(S, "_maximized", False)
    icon = HERE / "assets" / "vibe.ico"
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"),
                  icon=str(icon) if icon.exists() else None)


def main():
    global SERVER
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://localhost:{PORT}"
    try:
        server = SERVER = LocalServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # Already running: bring that window to the front instead of opening a second one.
        print(f"{APP_NAME} is already running; switching to it.")
        try:
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/api/focus", data=b"{}",
                                                          headers={"Content-Type": "application/json"}), timeout=3)
        except Exception:
            open_app_window(url)
        return
    server.daemon_threads = True
    threading.Thread(target=hw, daemon=True).start()  # detect hardware in the background
    print(f"{APP_NAME} {APP_VERSION} running at {url}")

    webview = None if "--no-browser" in sys.argv else load_webview()
    if webview:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        run_native(webview, url)
        # Window closed. If the game is still running, stay alive (headless) so it
        # isn't cut off, then exit when it closes.
        while S.game_running or S.busy:
            time.sleep(2)
        server.shutdown()
        print("Bye!")
        return

    if "--no-browser" not in sys.argv:
        proc = open_app_window(url)
        if proc:
            console_window(False)
            threading.Thread(target=watch_window, args=(proc,), daemon=True).start()
        else:
            print("Opened in your browser. Keep this window open while you use the launcher.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Bye!")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "launcher-error.log").write_text(err)
        except OSError:
            pass
        if FROZEN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "Vibe Launcher couldn't start. Details were saved to "
                                             f"{DATA_DIR / 'launcher-error.log'}", APP_NAME, 0x10)
            sys.exit(1)
        console_window(True)
        print(err)
        print("\nSomething went wrong - copy the text above if you need help.")
        _pause_and_exit()
