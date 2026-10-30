"""Backups for anything the optimizer or mod manager changes, plus options.txt editing.

Every change made by "Apply" is preceded by a snapshot, and "Restore" puts the
snapshot back. Nothing is changed silently.
"""
import json
import shutil
import time
import zipfile
from pathlib import Path


def read_options(path):
    """options.txt -> ordered dict of key -> raw string value."""
    opts = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                opts[k] = v
    return opts


def write_options(path, changes):
    """Update only the given keys, keeping every other line as-is.
    If options.txt doesn't exist yet (game never started), create it with just
    these keys; Minecraft fills in the rest on first start."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []
    todo = {k: str(v) for k, v in changes.items()}
    out = []
    for line in lines:
        k = line.split(":", 1)[0] if ":" in line else None
        if k in todo:
            out.append(f"{k}:{todo.pop(k)}")
        else:
            out.append(line)
    out += [f"{k}:{v}" for k, v in todo.items()]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".txt.vibe-tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(p)


class Backups:
    def __init__(self, root: Path):
        self.root = Path(root)

    def create(self, label, settings: dict, files=(), zip_dirs=()):
        """Snapshot launcher settings + given files (+ zipped dirs). Returns id."""
        bid = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000) % 1000:03d}"
        d = self.root / bid
        d.mkdir(parents=True, exist_ok=True)
        manifest = {"id": bid, "label": label, "time": time.time(), "files": [], "missing": [], "zips": []}
        (d / "settings.json").write_text(json.dumps(settings, indent=2))
        for i, f in enumerate(files):
            f = Path(f)
            if f.exists():
                shutil.copy2(f, d / f"file{i}")
                manifest["files"].append({"slot": f"file{i}", "path": str(f)})
            else:
                manifest["missing"].append(str(f))  # restore deletes what Apply created
        for i, folder in enumerate(zip_dirs):
            folder = Path(folder)
            if folder.is_dir():
                zp = d / f"dir{i}.zip"
                with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                    for p in folder.rglob("*"):
                        if p.is_file():
                            z.write(p, p.relative_to(folder))
                manifest["zips"].append({"zip": zp.name, "path": str(folder)})
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return bid

    def list(self, label_prefix=""):
        out = []
        if self.root.exists():
            for d in sorted(self.root.iterdir(), reverse=True):
                m = d / "manifest.json"
                if m.exists():
                    info = json.loads(m.read_text())
                    if info["label"].startswith(label_prefix):
                        out.append(info)
        return out

    def restore(self, bid):
        """Put files back; returns the saved launcher settings dict."""
        d = self.root / bid
        manifest = json.loads((d / "manifest.json").read_text())
        for f in manifest["files"]:
            Path(f["path"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d / f["slot"], f["path"])
        for path in manifest["missing"]:
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
        return json.loads((d / "settings.json").read_text())
