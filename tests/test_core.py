"""Tests for Vibe Launcher's critical logic. Run:  python -m unittest discover -s tests -v

These don't need the internet, a Microsoft account, or minecraft-launcher-lib
(a tiny fake is injected for the launcher-level tests)."""
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vibe_core import backups, crash, hardware, mods, util  # noqa: E402
from vibe_core.secure_store import AccountStore  # noqa: E402


def make_jar(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return path


def fabric_jar(d, fname, mod_id, name=None, depends=None, version="1.0", provides=None):
    meta = {"schemaVersion": 1, "id": mod_id, "name": name or mod_id, "version": version,
            "depends": depends or {}, "provides": provides or []}
    return make_jar(Path(d) / fname, {"fabric.mod.json": json.dumps(meta)})


# ---------------------------------------------------------------- util ---
class UtilTests(unittest.TestCase):
    def test_safe_filename_accepts_normal_jars(self):
        self.assertEqual(util.safe_filename("sodium-fabric-0.6.0+mc26.2.jar"), "sodium-fabric-0.6.0+mc26.2.jar")

    def test_safe_filename_blocks_traversal(self):
        for bad in ["../evil.jar", "a/b.jar", "..\\x.jar", "C:\\x.jar", "", "..", "x\x00.jar", "a:b.jar"]:
            with self.assertRaises(ValueError, msg=bad):
                util.safe_filename(bad)

    def test_is_within(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(util.is_within(Path(d) / "a" / "b.txt", d))
            self.assertFalse(util.is_within(Path(d) / ".." / "x", d))

    def test_redact_hides_tokens(self):
        self.assertEqual(util.redact("token=abcdefgh123 ok", ["abcdefgh123"]), "token=*** ok")
        self.assertEqual(util.redact("short", ["ab"]), "short")  # too short to be a token


# ------------------------------------------------------------ hardware ---
class RamTests(unittest.TestCase):
    def test_recommendations_scale_with_ram(self):
        self.assertEqual(hardware.recommend_ram(4096), 2048)
        self.assertEqual(hardware.recommend_ram(8192), 3072)
        self.assertEqual(hardware.recommend_ram(32768), 4096)
        self.assertGreater(hardware.recommend_ram(32768, "modpack"), hardware.recommend_ram(32768, "modded"))

    def test_recommendation_never_exceeds_safe_max(self):
        for total in (3072, 4096, 6144, 8192, 16384):
            for kind in ("vanilla", "modded", "modpack"):
                self.assertLessEqual(hardware.recommend_ram(total, kind), hardware.ram_limits(total)[0])

    def test_check_ram_levels(self):
        self.assertEqual(hardware.check_ram(4096, 16384)[0]["level"], "ok")
        self.assertEqual(hardware.check_ram(14336, 16384)[0]["level"], "warn")   # leaves < 4 GB for Windows
        self.assertEqual(hardware.check_ram(16000, 16384)[0]["level"], "error")  # beyond hard max
        self.assertEqual(hardware.check_ram(1024, 16384)[0]["level"], "warn")    # too little

    def test_clamp_prevents_destabilizing_allocation(self):
        self.assertEqual(hardware.clamp_ram(32768, 8192), 8192 - 1024)
        self.assertEqual(hardware.clamp_ram(256, 8192), 1024)


class JvmTests(unittest.TestCase):
    def test_heap_flags_and_order(self):
        a = hardware.jvm_args("balanced", 4096)
        self.assertEqual(a[:2], ["-Xmx4096M", "-Xms1024M"])
        self.assertNotIn("-XX:+UseZGC", a)

    def test_xms_never_above_xmx(self):
        self.assertIn("-Xms2048M", hardware.jvm_args("balanced", 2048, xms_mb=9999))

    def test_zgc_only_on_strong_machines_with_new_java(self):
        self.assertIn("-XX:+UseZGC", hardware.jvm_args("high", 8192, java_major=21, cores=8))
        self.assertNotIn("-XX:+UseZGC", hardware.jvm_args("high", 8192, java_major=17, cores=8))
        self.assertNotIn("-XX:+UseZGC", hardware.jvm_args("high", 4096, java_major=21, cores=16))
        self.assertIn("-XX:+UseG1GC", hardware.jvm_args("high", 4096, java_major=21, cores=16))

    def test_user_heap_flags_are_ignored_but_others_kept(self):
        a = hardware.jvm_args("balanced", 4096, extra="-Xmx99G -XX:+UseStringDeduplication")
        self.assertNotIn("-Xmx99G", a)
        self.assertIn("-XX:+UseStringDeduplication", a)

    def test_experimental_unlock_precedes_experimental_flags(self):
        a = hardware.jvm_args("maxfps", 4096)
        self.assertLess(a.index("-XX:+UnlockExperimentalVMOptions"), a.index("-XX:G1NewSizePercent=20"))

    def test_suggest_preset(self):
        self.assertEqual(hardware.suggest_preset({"ram_total_mb": 4096, "cores": 4}), "low")
        self.assertEqual(hardware.suggest_preset({"ram_total_mb": 32768, "cores": 12, "integrated_only": False}), "high")
        self.assertEqual(hardware.suggest_preset({"ram_total_mb": 32768, "cores": 12, "integrated_only": True}), "low")

    def test_integrated_gpu_detection(self):
        self.assertTrue(hardware.is_integrated("Intel(R) UHD Graphics 620"))
        self.assertTrue(hardware.is_integrated("AMD Radeon(TM) Graphics"))
        self.assertFalse(hardware.is_integrated("NVIDIA GeForce RTX 4070"))

    def test_detect_runs_here(self):
        info = hardware.detect()
        for k in ("cpu", "cores", "ram_total_mb", "gpus", "is_64bit"):
            self.assertIn(k, info)


# --------------------------------------------------------------- crash ---
class CrashTests(unittest.TestCase):
    def ids(self, text):
        return [f["id"] for f in crash.analyze(text)]

    def test_known_errors(self):
        self.assertIn("oom", self.ids("Exception in thread \"Render thread\" java.lang.OutOfMemoryError: Java heap space"))
        self.assertIn("heap_reserve", self.ids("Error: Could not reserve enough space for 8388608KB object heap"))
        self.assertIn("gl_driver", self.ids("GLFW error 65542: WGL: The driver does not appear to support OpenGL"))
        self.assertIn("java_version", self.ids("java.lang.UnsupportedClassVersionError: foo has been compiled by a more recent version of the Java Runtime"))
        self.assertIn("duplicate", self.ids("net.fabricmc.loader.impl.FormattedException: Found duplicate mods"))
        self.assertIn("mod_deps", self.ids("Incompatible mods found!\n - Mod 'Foo' (foo) 1.0 requires any version of fabric-api, which is missing!"))

    def test_mixin_names_culprit(self):
        f = crash.analyze("Mixin apply for mod coolmod failed coolmod.mixins.json:Foo from mod coolmod")
        self.assertEqual(f[0]["id"], "mixin")
        self.assertIn("coolmod", f[0]["fix"])

    def test_gpu_native_replaces_generic_native(self):
        txt = "# A fatal error has been detected by the Java Runtime Environment:\n# EXCEPTION_ACCESS_VIOLATION\n# C [atio6axx.dll+0x1234]"
        ids = self.ids(txt)
        self.assertIn("gpu_native", ids)
        self.assertNotIn("native_crash", ids)

    def test_unknown_crash_still_explained(self):
        s = crash.summarize(1, "nothing recognisable here")
        self.assertEqual(s["findings"][0]["id"], "unknown")
        self.assertEqual(crash.summarize(0, "clean exit")["findings"], [])

    def test_newest_report_only_after_launch(self):
        with tempfile.TemporaryDirectory() as d:
            cr = Path(d) / "crash-reports"
            cr.mkdir()
            old = cr / "crash-old.txt"
            old.write_text("old")
            os.utime(old, (1000, 1000))
            self.assertIsNone(crash.newest_report(d, 5000))
            new = cr / "crash-new.txt"
            new.write_text("new")
            self.assertEqual(crash.newest_report(d, 5000).name, "crash-new.txt")


# ---------------------------------------------------------------- mods ---
class ModFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "mods"
        self.d.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_fabric_and_neoforge_meta(self):
        fabric_jar(self.d, "sodium.jar", "sodium", "Sodium", {"minecraft": "26.2"}, "0.6.0")
        make_jar(self.d / "nf.jar", {"META-INF/neoforge.mods.toml":
                 '[[mods]]\nmodId="coolnf"\nversion="2.0"\ndisplayName="Cool NF"\n'
                 '[[dependencies.coolnf]]\nmodId="architectury"\ntype="required"\n'})
        ms = {m["id"]: m for m in mods.list_mods(self.d)}
        self.assertEqual(ms["sodium"]["version"], "0.6.0")
        self.assertEqual(ms["sodium"]["loader"], "fabric")
        self.assertEqual(ms["coolnf"]["loader"], "neoforge")
        self.assertIn("architectury", ms["coolnf"]["depends"])

    def test_corrupt_jar_does_not_crash(self):
        (self.d / "broken.jar").write_bytes(b"not a zip")
        m = mods.list_mods(self.d)[0]
        self.assertIn("error", m)
        self.assertTrue(any(p["kind"] == "unreadable" for p in mods.find_problems([m])))

    def test_problems_duplicates_conflicts_missing_deps(self):
        fabric_jar(self.d, "sodium-a.jar", "sodium")
        fabric_jar(self.d, "sodium-b.jar", "sodium")
        fabric_jar(self.d, "optifabric.jar", "optifabric")
        fabric_jar(self.d, "needsapi.jar", "needsapi", depends={"fabric-api": "*", "fabricloader": ">=0.16"})
        fabric_jar(self.d, "needsmodule.jar", "needsmodule", depends={"fabric-rendering-v1": "*"})
        kinds = [p["kind"] for p in mods.find_problems(mods.list_mods(self.d), "fabric")]
        self.assertIn("duplicate", kinds)
        self.assertIn("conflict", kinds)
        self.assertEqual(kinds.count("missing_dep"), 2)  # fabric-api, and the module (no fabric-api jar)
        fabric_jar(self.d, "fabric-api.jar", "fabric-api")
        kinds = [p["kind"] for p in mods.find_problems(mods.list_mods(self.d), "fabric")]
        self.assertNotIn("missing_dep", kinds)  # fabric-api provides the fabric-* modules

    def test_disabled_mods_are_ignored_by_checks(self):
        fabric_jar(self.d, "sodium-a.jar", "sodium")
        fabric_jar(self.d, "sodium-b.jar", "sodium")
        mods.set_enabled(self.d, "sodium-b.jar", False)
        self.assertTrue((self.d / "sodium-b.jar.disabled").exists())
        self.assertEqual(mods.find_problems(mods.list_mods(self.d)), [])
        mods.set_enabled(self.d, "sodium-b.jar.disabled", True)
        self.assertTrue((self.d / "sodium-b.jar").exists())

    def test_wrong_loader_warning(self):
        fabric_jar(self.d, "x.jar", "x")
        kinds = [p["kind"] for p in mods.find_problems(mods.list_mods(self.d), "neoforge")]
        self.assertIn("wrong_loader", kinds)

    def test_remove_moves_to_trash_and_rejects_traversal(self):
        fabric_jar(self.d, "x.jar", "x")
        trash = Path(self.tmp.name) / "trash"
        moved = mods.remove_mod(self.d, "x.jar", trash)
        self.assertTrue(Path(moved).exists())
        self.assertFalse((self.d / "x.jar").exists())
        with self.assertRaises(ValueError):
            mods.remove_mod(self.d, "../settings.json", trash)
        with self.assertRaises(ValueError):
            mods.set_enabled(self.d, "..\\x.jar", True)


def fake_version(pid, vnum, mc, loader, deps=(), vtype="release", fname=None):
    return {"project_id": pid, "version_number": vnum, "game_versions": [mc], "loaders": [loader],
            "version_type": vtype, "dependencies": list(deps),
            "files": [{"primary": True, "filename": fname or f"{pid}-{vnum}.jar",
                       "url": f"https://cdn.modrinth.com/data/{pid}/{pid}.jar", "hashes": {"sha512": "ab" * 64}}]}


class ModPlanTests(unittest.TestCase):
    def fake_api(self, path, params=None, retries=3, timeout=20):
        catalog = {
            "sodium": [fake_version("SOD", "0.6", "26.2", "fabric")],
            "lithium": [fake_version("LIT", "0.15", "26.2", "fabric",
                                     deps=[{"project_id": "FAPI", "dependency_type": "required"}])],
            "FAPI": [fake_version("FAPI", "0.120", "26.2", "fabric", fname="fabric-api-0.120.jar")],
            "oldmod": [fake_version("OLD", "1.0", "1.21.1", "fabric")],
            "betaonly": [fake_version("BETA", "0.1", "26.2", "fabric", vtype="beta")],
            "evil": [fake_version("EVIL", "1", "26.2", "fabric", fname="../../evil.jar")],
        }
        if path.startswith("/project/") and path.endswith("/version"):
            return catalog.get(path.split("/")[2], [])
        if path.startswith("/project/"):
            return {"FAPI": {"slug": "fabric-api"}}.get(path.split("/")[2], {})
        return None

    def test_dependencies_are_added(self):
        with mock.patch.object(mods, "_get_json", self.fake_api):
            plan = mods.plan_install(["lithium"], "26.2", "fabric")
        names = [f["filename"] for f in plan["files"]]
        self.assertEqual(names, ["LIT-0.15.jar", "fabric-api-0.120.jar"])
        self.assertEqual(plan["files"][1]["required_by"], "lithium")

    def test_installed_dependency_not_duplicated(self):
        with mock.patch.object(mods, "_get_json", self.fake_api):
            plan = mods.plan_install(["lithium"], "26.2", "fabric", installed_ids={"fabric-api"})
        self.assertEqual([f["filename"] for f in plan["files"]], ["LIT-0.15.jar"])

    def test_incompatible_versions_never_planned(self):
        with mock.patch.object(mods, "_get_json", self.fake_api):
            plan = mods.plan_install(["oldmod", "sodium"], "26.2", "fabric")
        self.assertEqual([f["project"] for f in plan["files"]], ["sodium"])
        self.assertEqual(plan["unavailable"][0]["project"], "oldmod")
        with mock.patch.object(mods, "_get_json", self.fake_api):
            self.assertEqual(mods.plan_install(["sodium"], "26.2", "neoforge")["files"], [])

    def test_beta_used_only_when_no_release(self):
        with mock.patch.object(mods, "_get_json", self.fake_api):
            self.assertEqual(mods.best_version("betaonly", "26.2", "fabric")["version_number"], "0.1")

    def test_unsafe_remote_filename_rejected(self):
        with mock.patch.object(mods, "_get_json", self.fake_api):
            with self.assertRaises(ValueError):
                mods.plan_install(["evil"], "26.2", "fabric")


class DownloadTests(unittest.TestCase):
    def item(self, data, sha=None, url="https://cdn.modrinth.com/data/x/x.jar"):
        return {"filename": "x.jar", "url": url, "sha512": sha or hashlib.sha512(data).hexdigest()}

    def fake_open(self, data):
        def opener(req, timeout=0):
            r = io.BytesIO(data)
            r.__enter__ = lambda s=r: s
            r.__exit__ = lambda *a: False
            return r
        return opener

    def test_verified_download(self):
        data = b"jar-bytes" * 100
        with tempfile.TemporaryDirectory() as d, mock.patch("urllib.request.urlopen", self.fake_open(data)), \
                mock.patch("time.sleep"):
            path, status = mods._download_verified(self.item(data), d)
            self.assertEqual(Path(path).read_bytes(), data)
            self.assertEqual(status, "installed")
            self.assertEqual(mods._download_verified(self.item(data), d)[1], "already installed")

    def test_checksum_mismatch_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("urllib.request.urlopen", self.fake_open(b"tampered")), \
                mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                mods._download_verified(self.item(b"x", sha="00" * 64), d)
            self.assertEqual(os.listdir(d), [])

    def test_unexpected_host_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                mods._download_verified(self.item(b"x", url="https://evil.example/x.jar"), d)


# ------------------------------------------------------------- backups ---
class BackupTests(unittest.TestCase):
    def test_write_options_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "options.txt"
            f.write_text("version:4000\nrenderDistance:24\nkey_key.jump:key.keyboard.space\n")
            backups.write_options(f, {"renderDistance": 8, "maxFps": 260})
            o = backups.read_options(f)
            self.assertEqual(o["renderDistance"], "8")
            self.assertEqual(o["maxFps"], "260")
            self.assertEqual(o["key_key.jump"], "key.keyboard.space")
            self.assertEqual(o["version"], "4000")

    def test_backup_and_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "options.txt"
            f.write_text("renderDistance:24\n")
            b = backups.Backups(Path(d) / "bk")
            bid = b.create("optimizer", {"ram_mb": 6144}, files=[f])
            backups.write_options(f, {"renderDistance": 6})
            saved = b.restore(bid)
            self.assertEqual(saved["ram_mb"], 6144)
            self.assertEqual(backups.read_options(f)["renderDistance"], "24")

    def test_restore_removes_file_that_apply_created(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "options.txt"
            b = backups.Backups(Path(d) / "bk")
            bid = b.create("optimizer", {}, files=[f])
            backups.write_options(f, {"renderDistance": 6})
            b.restore(bid)
            self.assertFalse(f.exists())


# --------------------------------------------------------- secure store ---
class StoreTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "file fallback is for non-Windows")
    def test_file_fallback_is_private(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(AccountStore, "_keyring", lambda self: None):
            s = AccountStore(d)
            s.save({"name": "Steve", "refresh_token": "r" * 20})
            self.assertEqual(s.load()["name"], "Steve")
            self.assertEqual(os.stat(s.plain_file).st_mode & 0o777, 0o600)
            s.clear()
            self.assertIsNone(s.load())


# ------------------------------------------------------------ launcher ---
def load_launcher(tmp):
    """Import launcher.py with a fake minecraft_launcher_lib and a temp home."""
    fake = types.ModuleType("minecraft_launcher_lib")
    fake.utils = types.SimpleNamespace(get_minecraft_directory=lambda: str(Path(tmp) / "mc"),
                                       get_version_list=lambda: [], get_installed_versions=lambda d: [])
    for n in ("install", "command", "microsoft_account", "mod_loader"):
        setattr(fake, n, types.SimpleNamespace())
    sys.modules["minecraft_launcher_lib"] = fake
    with mock.patch.object(Path, "home", lambda: Path(tmp)):
        sys.modules.pop("launcher", None)
        import launcher
        return launcher


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.L = load_launcher(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_version_filter(self):
        ok = ["26.1", "26.2.1", "26.3-snapshot-2", "fabric-loader-0.17.2-26.2"]
        bad = ["1.21.10", "1.20.1", "fabric-loader-0.17.2-1.21.10", "25w41a", "b1.7.3"]
        self.assertTrue(all(self.L.is_new_enough(v) for v in ok))
        self.assertFalse(any(self.L.is_new_enough(v) for v in bad))

    def test_migration_keeps_old_settings(self):
        s = self.L.migrate({"last_version": "26.2", "ram_mb": 6144, "profiles": []})
        self.assertEqual(s["profiles"][0]["version"], "26.2")
        self.assertEqual(s["ram_mb"], 6144)
        self.assertEqual(s["profiles"][0]["game_dir"], "")  # Default keeps the main .minecraft

    def test_fast_path_requires_marker_and_files(self):
        L = self.L
        mc = Path(self.tmp.name) / "mc"
        vdir = mc / "versions" / "26.2"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "26.2.json").write_text(json.dumps({"id": "26.2", "javaVersion": {"majorVersion": 25, "component": "java-runtime-x"}}))
        (vdir / "26.2.jar").write_bytes(b"jar")
        self.assertFalse(L.install_complete(mc, "26.2"))       # no marker yet
        L.mark_installed("26.2")
        self.assertFalse(L.install_complete(mc, "26.2"))       # Java runtime missing
        (mc / "runtime" / "java-runtime-x").mkdir(parents=True)
        self.assertTrue(L.install_complete(mc, "26.2"))
        self.assertEqual(L.java_requirement(mc, "26.2"), (25, "java-runtime-x"))
        (vdir / "26.2.jar").unlink()
        self.assertFalse(L.install_complete(mc, "26.2"))       # deleted jar -> full install again

    def test_loader_version_inherits_java(self):
        L = self.L
        mc = Path(self.tmp.name) / "mc"
        fdir = mc / "versions" / "fabric-loader-0.17-26.2"
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "fabric-loader-0.17-26.2.json").write_text(json.dumps({"id": "x", "inheritsFrom": "26.2"}))
        (mc / "versions" / "26.2").mkdir(parents=True, exist_ok=True)
        (mc / "versions" / "26.2" / "26.2.json").write_text(json.dumps({"javaVersion": {"majorVersion": 25, "component": "c"}}))
        self.assertEqual(L.java_requirement(mc, "fabric-loader-0.17-26.2")[0], 25)

    def test_modded_profile_gets_own_folder_and_default_stays_vanilla(self):
        L = self.L
        p = L.save_profile({"name": "Fabric Perf", "loader": "fabric", "version": "26.2"})
        self.assertIn("vibe-profiles", p["game_dir"])
        with self.assertRaises(ValueError):
            L.save_profile({"id": "default", "name": "Default", "loader": "fabric", "version": "26.2"})
        with self.assertRaises(ValueError):
            L.save_profile({"name": "Old", "loader": "vanilla", "version": "1.21.10"})

    def test_state_log_is_incremental_and_redacted(self):
        L = self.L
        L.S.account = {"name": "Steve", "access_token": "SECRET-TOKEN-123", "refresh_token": "REFRESH-456789"}
        start = L.S.log_base + len(L.S.log)
        L.S.push_log("launching with SECRET-TOKEN-123")
        snap = L.state_snapshot(start, 0)
        self.assertEqual(snap["log"], ["launching with ***"])
        self.assertEqual(L.state_snapshot(snap["log_next"], 0)["log"], [])
        L.S.account = None

    def test_open_path_restricted(self):
        with self.assertRaises(ValueError):
            self.L.open_path("/etc/passwd")


# ------------------------------------------------- official launcher ---
from vibe_core import official  # noqa: E402


class OfficialLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mc = Path(self.tmp.name) / ".minecraft"
        self.mc.mkdir()
        self.original = {"profiles": {"abc123": {"name": "My Forge Pack", "type": "custom", "lastVersionId": "1.20.1-forge",
                                                 "javaArgs": "-Xmx8G"},
                                      "latest-release": {"type": "latest-release", "name": ""}},
                         "settings": {"enableSnapshots": False, "crashAssistance": True}, "version": 3}
        (self.mc / "launcher_profiles.json").write_text(json.dumps(self.original))

    def tearDown(self):
        self.tmp.cleanup()

    def test_adds_installation_and_keeps_everything_else(self):
        bk = Path(self.tmp.name) / "bk"
        official.upsert_installation(self.mc, "fabric-perf", "Vibe · Fabric Perf", "fabric-loader-0.17-26.2",
                                     game_dir="/games/fp", java_args=["-Xmx4096M", "-XX:+UseG1GC"], backup_dir=bk)
        d = json.loads((self.mc / "launcher_profiles.json").read_text())
        self.assertEqual(d["profiles"]["abc123"], self.original["profiles"]["abc123"])
        self.assertEqual(d["settings"], self.original["settings"])
        e = d["profiles"]["vibe-fabric-perf"]
        self.assertEqual(e["lastVersionId"], "fabric-loader-0.17-26.2")
        self.assertEqual(e["gameDir"], "/games/fp")
        self.assertEqual(e["javaArgs"], "-Xmx4096M -XX:+UseG1GC")
        self.assertEqual(e["type"], "custom")
        self.assertTrue((bk / "original-launcher_profiles.json").exists())

    def test_update_is_idempotent_and_vanilla_has_no_gamedir(self):
        official.upsert_installation(self.mc, "default", "Vibe · Default", "26.2", game_dir="/x")
        official.upsert_installation(self.mc, "default", "Vibe · Default", "26.2", game_dir=None)
        d = json.loads((self.mc / "launcher_profiles.json").read_text())
        self.assertEqual(sum(1 for k in d["profiles"] if k.startswith("vibe-")), 1)
        self.assertNotIn("gameDir", d["profiles"]["vibe-default"])

    def test_store_edition_file_also_updated(self):
        (self.mc / "launcher_profiles_microsoft_store.json").write_text(json.dumps({"profiles": {}}))
        files = official.upsert_installation(self.mc, "default", "Vibe · Default", "26.2")
        self.assertEqual(len(files), 2)

    def test_not_set_up(self):
        (self.mc / "launcher_profiles.json").unlink()
        with self.assertRaises(official.LauncherNotSetUp):
            official.upsert_installation(self.mc, "default", "Vibe · Default", "26.2")

    def test_remove_only_touches_vibe_entries(self):
        official.upsert_installation(self.mc, "x", "Vibe · X", "26.2")
        official.remove_installation(self.mc, "x")
        official.remove_installation(self.mc, "abc123")  # becomes vibe-abc123: not present, nothing removed
        d = json.loads((self.mc / "launcher_profiles.json").read_text())
        self.assertNotIn("vibe-x", d["profiles"])
        self.assertIn("abc123", d["profiles"])

    def test_find_launcher_custom_path(self):
        exe = Path(self.tmp.name) / "MinecraftLauncher.exe"
        exe.write_text("")
        self.assertEqual(official.find_launcher(str(exe)), ("exe", str(exe)))


if __name__ == "__main__":
    unittest.main()
