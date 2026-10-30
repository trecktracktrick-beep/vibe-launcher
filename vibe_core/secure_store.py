"""Store the Microsoft session (refresh/access tokens) using the OS's protection.

- Windows: DPAPI (CryptProtectData), so only your Windows user can decrypt it.
- macOS/Linux: the system keyring via the `keyring` package when installed,
  otherwise a file readable only by your user (chmod 600).
"""
import json
import os
import sys
from pathlib import Path

SERVICE = "VibeLauncher"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    crypt32 = ctypes.windll.crypt32
    UI_FORBIDDEN = 0x1
    if encrypt:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "VibeLauncher session", None, None, None,
                                      UI_FORBIDDEN, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                        UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


class AccountStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.enc_file = self.dir / "account.dat"     # Windows, DPAPI-encrypted
        self.plain_file = self.dir / "account.json"  # fallback / legacy (v1.0)

    def _keyring(self):
        if sys.platform == "win32":
            return None
        try:
            import keyring  # optional dependency
            keyring.get_keyring()
            return keyring
        except Exception:
            return None

    def method(self):
        if sys.platform == "win32":
            return "Windows DPAPI (encrypted for your Windows user)"
        return "System keyring" if self._keyring() else "Private file (readable only by your user)"

    def load(self):
        try:
            if sys.platform == "win32" and self.enc_file.exists():
                return json.loads(_dpapi(self.enc_file.read_bytes(), False))
            kr = self._keyring()
            if kr:
                raw = kr.get_password(SERVICE, "account")
                if raw:
                    return json.loads(raw)
            if self.plain_file.exists():
                acct = json.loads(self.plain_file.read_text())
                # Migrate v1.0's plain file to the protected store.
                if sys.platform == "win32" or kr:
                    self.save(acct)
                return acct
        except Exception:
            return None
        return None

    def save(self, acct):
        self.dir.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(acct)
        if sys.platform == "win32":
            tmp = self.enc_file.with_suffix(".tmp")
            tmp.write_bytes(_dpapi(raw.encode(), True))
            os.replace(tmp, self.enc_file)
            self._remove_plain()
            return
        kr = self._keyring()
        if kr:
            kr.set_password(SERVICE, "account", raw)
            self._remove_plain()
            return
        fd = os.open(self.plain_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(raw)

    def clear(self):
        for p in (self.enc_file, self.plain_file):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        kr = self._keyring()
        if kr:
            try:
                kr.delete_password(SERVICE, "account")
            except Exception:
                pass

    def _remove_plain(self):
        try:
            self.plain_file.unlink()
        except FileNotFoundError:
            pass
