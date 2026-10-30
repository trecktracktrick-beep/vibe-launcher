"""Mod management: reading installed mods, finding problems, and installing from
Modrinth with version/loader compatibility checks, dependency resolution and
SHA-512 verification.

Modrinth API rules respected: identifying User-Agent, modest request rate,
downloads only from the URLs the API returns.
"""
import hashlib
import json
import re
import shutil
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .util import safe_filename

API = "https://api.modrinth.com/v2"
USER_AGENT = "VibeLauncher/1.1 (personal Minecraft launcher)"
LOADERS = ("fabric", "neoforge", "forge")

PERFORMANCE_MODS = [
    {"slug": "sodium", "name": "Sodium",
     "what": "Replaces the renderer. Usually the single biggest FPS improvement."},
    {"slug": "lithium", "name": "Lithium",
     "what": "Optimizes game logic (mob AI, physics, chunk ticking). Helps TPS in singleplayer."},
    {"slug": "ferrite-core", "name": "FerriteCore",
     "what": "Cuts memory use, which means fewer garbage-collection pauses."},
    {"slug": "immediatelyfast", "name": "ImmediatelyFast",
     "what": "Speeds up drawing of text, HUD, entities and particles."},
]

# Pairs of mod ids that must not be installed together.
CONFLICTS = [
    ({"sodium"}, {"optifine", "optifabric"}, "OptiFine and Sodium both replace the renderer. Keep Sodium."),
    ({"sodium"}, {"embeddium", "rubidium"}, "Embeddium/Rubidium are Sodium ports; only one renderer mod can be used."),
    ({"lithium"}, {"canary", "radium"}, "Canary/Radium are Lithium ports; use only one."),
]

# Dependencies that are part of the loader/game, not separate mods.
BUILTIN_IDS = {"minecraft", "java", "fabricloader", "fabric-loader", "fabric", "quilt_loader",
               "forge", "neoforge", "mixinextras"}


# ----------------------------------------------------------------- http ---
def _get_json(path, params=None, retries=3, timeout=20):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            if e.code == 429:
                time.sleep(int(e.headers.get("X-Ratelimit-Reset", "5") or 5))
                continue
        except Exception as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Modrinth request failed: {last}")


def _compatible(version, mc, loader):
    return mc in version.get("game_versions", []) and loader in version.get("loaders", [])


def best_version(project, mc, loader, quick=False):
    """Newest release (or beta if no release) of a project that supports exactly
    this Minecraft version and loader. None if there is none."""
    vs = _get_json(f"/project/{urllib.parse.quote(project)}/version",
                   {"loaders": json.dumps([loader]), "game_versions": json.dumps([mc])},
                   **({"retries": 1, "timeout": 8} if quick else {})) or []
    vs = [v for v in vs if _compatible(v, mc, loader)]  # double-check API filtering
    for kind in ("release", "beta"):
        for v in vs:
            if v.get("version_type") == kind:
                return v
    return None


def primary_file(version):
    files = version.get("files") or []
    return next((f for f in files if f.get("primary")), files[0] if files else None)


def search(query, mc, loader, limit=20):
    facets = [[f"categories:{loader}"], [f"versions:{mc}"], ["project_type:mod"]]
    res = _get_json("/search", {"query": query, "limit": limit, "facets": json.dumps(facets)}, retries=1, timeout=10) or {}
    return [{"slug": h["slug"], "name": h["title"], "what": h.get("description", ""),
             "downloads": h.get("downloads", 0), "icon": h.get("icon_url") or ""} for h in res.get("hits", [])]


def check_compat(slugs, mc, loader):
    """{slug: {'ok': bool, 'version': str}} for a list of projects."""
    def one(slug):
        try:
            v = best_version(slug, mc, loader, quick=True)
            return slug, {"ok": bool(v), "version": v["version_number"] if v else ""}
        except Exception as e:
            return slug, {"ok": False, "version": "", "error": str(e)}
    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, slugs))


# ------------------------------------------------------- install plan ---
def plan_install(slugs, mc, loader, installed_ids=()):
    """Resolve requested projects plus their required dependencies.
    Returns {'files': [...], 'unavailable': [...], 'notes': [...]}. Nothing is
    downloaded here and nothing incompatible is ever added to the plan."""
    have = set(installed_ids)
    planned, files, unavailable, notes = set(), [], [], []
    queue = [(s, None) for s in slugs]
    while queue:
        project, required_by = queue.pop(0)
        if project in planned or project in have:
            continue
        planned.add(project)
        slug = ""
        if required_by is not None:
            # Dependencies arrive as project ids; look up the slug so a manually
            # installed copy (e.g. fabric-api.jar) is recognised and not duplicated.
            info = _get_json(f"/project/{urllib.parse.quote(project)}") or {}
            slug = info.get("slug", "")
            if slug and (slug in have or slug.replace("-", "") in have):
                continue
        v = best_version(project, mc, loader)
        if not v:
            unavailable.append({"project": project, "required_by": required_by})
            continue
        proj_slug = slug or project
        f = primary_file(v)
        if not f or "sha512" not in f.get("hashes", {}):
            unavailable.append({"project": project, "required_by": required_by, "reason": "no verifiable file"})
            continue
        files.append({"project": proj_slug, "project_id": v["project_id"], "version": v["version_number"],
                      "filename": safe_filename(f["filename"]), "url": f["url"],
                      "sha512": f["hashes"]["sha512"], "size": f.get("size", 0), "required_by": required_by})
        have.add(v["project_id"])
        for d in v.get("dependencies", []):
            if d.get("dependency_type") == "required" and d.get("project_id"):
                queue.append((d["project_id"], proj_slug))
            elif d.get("dependency_type") == "incompatible" and d.get("project_id"):
                notes.append(f"{proj_slug} declares an incompatibility with project {d['project_id']}.")
    return {"files": files, "unavailable": unavailable, "notes": notes}


def _download_verified(item, mods_dir, retries=3):
    dest = Path(mods_dir) / item["filename"]
    if dest.exists() and _sha512(dest) == item["sha512"]:
        return dest, "already installed"
    if not item["url"].startswith("https://cdn.modrinth.com/"):
        raise RuntimeError(f"Refusing download from unexpected host: {item['url']}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(item["url"], headers={"User-Agent": USER_AGENT})
            h = hashlib.sha512()
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
                while chunk := r.read(1 << 16):
                    h.update(chunk)
                    out.write(chunk)
            if h.hexdigest() != item["sha512"]:
                raise RuntimeError("checksum mismatch")
            tmp.replace(dest)
            return dest, "installed"
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    raise RuntimeError(f"{item['filename']}: {last}")


def install_plan(plan, mods_dir, progress=None, workers=4):
    Path(mods_dir).mkdir(parents=True, exist_ok=True)
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_download_verified, it, mods_dir): it for it in plan["files"]}
        for fut in futs:
            it = futs[fut]
            try:
                _, status = fut.result()
                results.append({"file": it["filename"], "status": status})
            except Exception as e:
                results.append({"file": it["filename"], "status": "failed", "error": str(e)})
            done += 1
            if progress:
                progress(done, len(plan["files"]), it["filename"])
    _record_sources(mods_dir, plan["files"])
    return results


def _record_sources(mods_dir, files):
    """Remember which Modrinth project each jar came from (for dependency checks)."""
    p = Path(mods_dir) / ".vibe-sources.json"
    try:
        data = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        data = {}
    for f in files:
        data[f["filename"]] = {"project": f["project"], "project_id": f["project_id"], "version": f["version"]}
    p.write_text(json.dumps(data, indent=2))


def _sha512(path):
    h = hashlib.sha512()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------- installed mods ---
def read_mod_meta(jar):
    """Read id/name/version/deps from a mod jar without extracting it."""
    meta = {"id": "", "name": Path(jar).name, "version": "", "loader": "", "depends": {}, "provides": []}
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names:
                d = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"), strict=False)
                meta.update(id=d.get("id", ""), name=d.get("name") or d.get("id", ""),
                            version=str(d.get("version", "")), loader="fabric",
                            depends={k: v for k, v in (d.get("depends") or {}).items()},
                            provides=list(d.get("provides") or []))
            elif "quilt.mod.json" in names:
                d = json.loads(z.read("quilt.mod.json").decode("utf-8", "replace"))
                ql = d.get("quilt_loader", {})
                meta.update(id=ql.get("id", ""), name=(ql.get("metadata") or {}).get("name") or ql.get("id", ""),
                            version=str(ql.get("version", "")), loader="quilt")
            else:
                for toml_name, loader in (("META-INF/neoforge.mods.toml", "neoforge"), ("META-INF/mods.toml", "forge")):
                    if toml_name in names:
                        t = z.read(toml_name).decode("utf-8", "replace")
                        mid = re.search(r'^\s*modId\s*=\s*"([^"]+)"', t, re.M)
                        name = re.search(r'^\s*displayName\s*=\s*"([^"]+)"', t, re.M)
                        ver = re.search(r'^\s*version\s*=\s*"([^"]+)"', t, re.M)
                        meta.update(id=mid.group(1) if mid else "", name=name.group(1) if name else meta["name"],
                                    version=ver.group(1) if ver else "", loader=loader)
                        deps = {}
                        for block in re.split(r"\[\[dependencies\.", t)[1:]:
                            dm = re.search(r'modId\s*=\s*"([^"]+)"', block)
                            req = re.search(r'(?:mandatory\s*=\s*true|type\s*=\s*"required")', block)
                            if dm and req:
                                deps[dm.group(1)] = "*"
                        meta["depends"] = deps
                        break
    except Exception as e:
        meta["error"] = f"Unreadable jar ({e.__class__.__name__})"
    if not meta["id"]:
        meta["id"] = re.sub(r"[-_ ]?\d.*$", "", Path(jar).name.lower().replace(".jar", "").replace(".disabled", "")) or Path(jar).name
    return meta


def list_mods(mods_dir):
    d = Path(mods_dir)
    if not d.is_dir():
        return []
    try:
        sources = json.loads((d / ".vibe-sources.json").read_text())
    except Exception:
        sources = {}
    out = []
    for p in sorted(d.iterdir(), key=lambda p: p.name.lower()):
        if p.is_file() and (p.name.endswith(".jar") or p.name.endswith(".jar.disabled")):
            m = read_mod_meta(p)
            enabled = p.name.endswith(".jar")
            src = sources.get(p.name if enabled else p.name[:-len(".disabled")], {})
            m.update(file=p.name, enabled=enabled, size=p.stat().st_size, project=src.get("project", ""))
            out.append(m)
    return out


def find_problems(mods, loader=""):
    """Duplicates, known conflicts, missing required deps, wrong-loader jars."""
    active = [m for m in mods if m["enabled"]]
    problems = []
    by_id = {}
    for m in active:
        by_id.setdefault(m["id"], []).append(m)
    for mid, ms in by_id.items():
        if len(ms) > 1:
            problems.append({"level": "error", "kind": "duplicate",
                             "msg": f"'{ms[0]['name']}' is installed {len(ms)} times ({', '.join(x['file'] for x in ms)}). Remove the older one."})
    ids = set(by_id) | {p for m in active for p in m.get("provides", [])}
    for a, b, why in CONFLICTS:
        if ids & a and ids & b:
            problems.append({"level": "error", "kind": "conflict", "msg": why})
    for m in active:
        for dep in m.get("depends", {}):
            if dep in BUILTIN_IDS or dep in ids:
                continue
            # Fabric API is split into many 'fabric-*' modules all provided by the fabric-api jar.
            if dep.startswith("fabric-") and ("fabric-api" in ids or "fabric" in ids):
                continue
            problems.append({"level": "error", "kind": "missing_dep", "mod": m["id"], "dep": dep,
                             "msg": f"'{m['name']}' needs '{dep}', which isn't installed."})
        if loader and m.get("loader") and m["loader"] not in (loader, "quilt" if loader == "fabric" else ""):
            if not (loader == "neoforge" and m["loader"] == "forge"):
                problems.append({"level": "warn", "kind": "wrong_loader",
                                 "msg": f"'{m['name']}' is a {m['loader']} mod but this profile uses {loader}. It won't load."})
        if m.get("error"):
            problems.append({"level": "warn", "kind": "unreadable", "msg": f"{m['file']}: {m['error']}"})
    return problems


def set_enabled(mods_dir, filename, enabled):
    filename = safe_filename(filename)
    d = Path(mods_dir)
    src = d / filename
    if not src.exists():
        raise FileNotFoundError(filename)
    if enabled and filename.endswith(".jar.disabled"):
        src.rename(d / filename[:-len(".disabled")])
    elif not enabled and filename.endswith(".jar"):
        src.rename(d / (filename + ".disabled"))


def remove_mod(mods_dir, filename, trash_dir):
    """Moves the jar to the launcher's trash folder instead of deleting it."""
    filename = safe_filename(filename)
    src = Path(mods_dir) / filename
    if not src.exists():
        raise FileNotFoundError(filename)
    t = Path(trash_dir) / time.strftime("%Y%m%d-%H%M%S")
    t.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(t / filename))
    return str(t / filename)
