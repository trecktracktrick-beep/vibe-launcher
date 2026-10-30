"""Small safety helpers: file-name validation, path containment, secret redaction."""
import os
import re
from pathlib import Path

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._+\-()\[\] ]{1,200}$")


def safe_filename(name: str) -> str:
    """Return a plain file name or raise ValueError. Blocks path traversal
    ('../x', 'a/b', 'C:\\x') and odd characters coming from remote APIs."""
    if not isinstance(name, str):
        raise ValueError("file name must be a string")
    base = os.path.basename(name.replace("\\", "/"))
    if base != name or base in ("", ".", "..") or not _SAFE_NAME.match(base) or ":" in base:
        raise ValueError(f"Unsafe file name: {name!r}")
    return base


def is_within(path, root) -> bool:
    """True if `path` resolves to `root` or somewhere inside it."""
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def redact(text: str, secrets) -> str:
    """Replace any secret (access/refresh tokens) appearing in text."""
    for s in secrets:
        if s and len(s) >= 8 and s in text:
            text = text.replace(s, "***")
    return text


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "profile"
