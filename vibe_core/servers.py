"""Multiplayer servers: live status (Server List Ping), SRV lookup and the
in-game server list (servers.dat).

Nothing here needs a Minecraft account. Pinging only asks a server for the
same public info the Multiplayer screen shows (description, players, icon).
"""
import base64
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = 25565

# Well-known public networks with survival modes. These are suggestions only;
# the launcher shows their live status, and you can remove them or add your own.
SUGGESTED = [
    {"id": "vanillarealms", "name": "Vanilla Realms", "address": "play.vanillarealms.com",
     "tags": ["Vanilla survival"]},
    {"id": "manacube", "name": "ManaCube", "address": "play.manacube.com",
     "tags": ["Survival", "SkyBlock", "Parkour"]},
    {"id": "opblocks", "name": "OPBlocks", "address": "play.opblocks.com",
     "tags": ["Survival", "SkyBlock", "Prison"]},
    {"id": "pikanetwork", "name": "PikaNetwork", "address": "play.pika-network.net",
     "tags": ["Survival", "SkyBlock", "BedWars"]},
    {"id": "lemoncloud", "name": "LemonCloud", "address": "play.lemoncloud.net",
     "tags": ["Survival", "SkyBlock", "Factions"]},
    {"id": "hypixel", "name": "Hypixel", "address": "mc.hypixel.net",
     "tags": ["SkyBlock", "SMP", "Minigames"]},
    {"id": "minehut", "name": "Minehut", "address": "minehut.com",
     "tags": ["Community SMPs"]},
]

_HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ------------------------------------------------------------- addresses ---
def parse_address(text):
    """'play.example.net', 'play.example.net:25570', '1.2.3.4', '[::1]:25565'
    -> (host, port or None). Raises ValueError for anything else."""
    s = str(text or "").strip()
    if not s or len(s) > 260 or any(c.isspace() for c in s):
        raise ValueError("Enter a server address like play.example.net or play.example.net:25565")
    port = None
    if s.startswith("["):
        m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?", s)
        if not m:
            raise ValueError("That IPv6 address doesn't look right.")
        host, port = m.group(1), m.group(2)
    elif s.count(":") == 1:
        host, port = s.split(":")
    else:
        host = s
    if port is not None:
        if not str(port).isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("The port must be a number from 1 to 65535.")
        port = int(port)
    if not s.startswith("[") and not _HOST_RE.match(host):
        raise ValueError("That server address doesn't look right.")
    return host.rstrip(".").lower(), port


def format_address(host, port):
    h = f"[{host}]" if ":" in host else host
    return h if port in (None, DEFAULT_PORT) else f"{h}:{port}"


_srv_cache = {}


def resolve_srv(host, timeout=3):
    """Look up the _minecraft._tcp SRV record (many servers use one).
    Returns (target_host, port) or None. Uses the system's nslookup so we
    ask the same DNS server Windows does."""
    if host in _srv_cache and time.time() - _srv_cache[host][0] < 600:
        return _srv_cache[host][1]
    found = None
    try:
        out = subprocess.run(["nslookup", "-type=SRV", f"_minecraft._tcp.{host}"],
                             capture_output=True, text=True, timeout=timeout,
                             creationflags=_NO_WINDOW).stdout
        found = parse_nslookup_srv(out)
    except (OSError, subprocess.SubprocessError):
        pass
    _srv_cache[host] = (time.time(), found)
    return found


def parse_nslookup_srv(out):
    """Parse nslookup's SRV output (Windows and Unix formats)."""
    m = re.search(r"port\s*=\s*(\d+).*?svr hostname\s*=\s*(\S+)", out or "", re.S | re.I)
    if m:
        return m.group(2).rstrip("."), int(m.group(1))
    m = re.search(r"service\s*=\s*\d+\s+\d+\s+(\d+)\s+(\S+)", out or "", re.I)
    if m:
        return m.group(2).rstrip("."), int(m.group(1))
    return None


# ------------------------------------------------------ server list ping ---
def _varint(n):
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Server closed the connection")
        buf += chunk
    return bytes(buf)


def _read_varint(read):
    num = shift = 0
    for _ in range(5):
        b = read(1)[0]
        num |= (b & 0x7F) << shift
        if not b & 0x80:
            return num - (1 << 32) if num & (1 << 31) else num
        shift += 7
    raise ValueError("VarInt too long")


def _packet(pid, payload=b""):
    body = _varint(pid) + payload
    return _varint(len(body)) + body


def _string(s):
    b = s.encode("utf-8")
    return _varint(len(b)) + b


def chat_to_text(c):
    """Turn a chat component (str / dict / list) into plain text without § codes."""
    if c is None:
        return ""
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        text = "".join(chat_to_text(x) for x in c)
    elif isinstance(c, dict):
        text = str(c.get("text", "") or c.get("translate", "") or "")
        text += "".join(chat_to_text(x) for x in c.get("extra", []) or [])
    else:
        text = str(c)
    return re.sub(r"§.", "", text)


def _clean_favicon(f):
    """Only accept a plain base64 PNG data URI (it ends up in an <img src>)."""
    if not isinstance(f, str):
        return ""
    f = f.replace("\n", "")
    pre = "data:image/png;base64,"
    if not f.startswith(pre) or len(f) > 200_000 or not re.fullmatch(r"[A-Za-z0-9+/=]*", f[len(pre):]):
        return ""
    return f


def parse_status(raw):
    """Server status JSON -> the fields the launcher shows."""
    d = json.loads(raw)
    players = d.get("players") or {}
    ver = d.get("version") or {}
    sample = [p.get("name", "") for p in (players.get("sample") or []) if isinstance(p, dict)]
    return {
        "motd": chat_to_text(d.get("description")).strip()[:300],
        "online_players": int(players.get("online") or 0),
        "max_players": int(players.get("max") or 0),
        "version": chat_to_text(ver.get("name"))[:60],
        "protocol": ver.get("protocol"),
        "favicon": _clean_favicon(d.get("favicon")),
        "sample": [re.sub(r"§.", "", s)[:32] for s in sample][:8],
    }


def ping(address, timeout=4.0):
    """Ask a server for its status. Never raises; returns {"online": False, "error": ...}
    when it can't be reached."""
    try:
        host, port = parse_address(address)
    except ValueError as e:
        return {"online": False, "error": str(e)}
    conn_host, conn_port = host, port or DEFAULT_PORT
    if port is None and not re.fullmatch(r"[\d.]+", host):
        srv = resolve_srv(host)
        if srv:
            conn_host, conn_port = srv
    started = time.perf_counter()
    try:
        with socket.create_connection((conn_host, conn_port), timeout=timeout) as s:
            s.settimeout(timeout)
            hs = _varint(0xFFFFFFFF) + _string(host) + struct.pack(">H", conn_port) + _varint(1)
            s.sendall(_packet(0x00, hs) + _packet(0x00))
            read = lambda n: _read_exact(s, n)  # noqa: E731
            length = _read_varint(read)
            if not 0 < length < 2_000_000:
                raise ValueError("Unexpected reply")
            data = io.BytesIO(_read_exact(s, length))
            if _read_varint(data.read) != 0x00:
                raise ValueError("Unexpected reply")
            n = _read_varint(data.read)
            info = parse_status(data.read(n).decode("utf-8", "replace"))
            latency = (time.perf_counter() - started) * 1000
            try:  # a proper round trip for the ping value; some servers skip it
                t0 = time.perf_counter()
                s.sendall(_packet(0x01, struct.pack(">q", 1)))
                _read_varint(read)
                _read_exact(s, 9)
                latency = (time.perf_counter() - t0) * 1000
            except (OSError, ValueError, ConnectionError):
                pass
    except socket.gaierror:
        return {"online": False, "error": "Address not found"}
    except (socket.timeout, TimeoutError):
        return {"online": False, "error": "No reply (timed out)"}
    except (OSError, ValueError, ConnectionError) as e:
        return {"online": False, "error": type(e).__name__ if not str(e) else str(e)[:120]}
    return {"online": True, "ping_ms": round(latency), **info}


# ------------------------------------------------------------------- NBT ---
# servers.dat is an uncompressed NBT file. A tiny reader/writer is enough to
# keep every existing entry (and any unknown fields) exactly as it was.
END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY = range(13)
_FMT = {BYTE: ">b", SHORT: ">h", INT: ">i", LONG: ">q", FLOAT: ">f", DOUBLE: ">d"}


def _r(f, fmt):
    size = struct.calcsize(fmt)
    b = f.read(size)
    if len(b) != size:
        raise ValueError("servers.dat is truncated")
    return struct.unpack(fmt, b)[0]


def _r_str(f):
    n = _r(f, ">H")
    return f.read(n).decode("utf-8", "replace")


def _r_payload(f, t, depth=0):
    if depth > 64:
        raise ValueError("servers.dat is nested too deeply")
    if t in _FMT:
        return _r(f, _FMT[t])
    if t == STRING:
        return _r_str(f)
    if t == BYTE_ARRAY:
        return f.read(_r(f, ">i"))
    if t in (INT_ARRAY, LONG_ARRAY):
        n = _r(f, ">i")
        return [_r(f, ">i" if t == INT_ARRAY else ">q") for _ in range(n)]
    if t == LIST:
        et, n = _r(f, ">b"), _r(f, ">i")
        return (et, [_r_payload(f, et, depth + 1) for _ in range(max(0, n))])
    if t == COMPOUND:
        out = {}
        while True:
            tt = _r(f, ">b")
            if tt == END:
                return out
            name = _r_str(f)
            out[name] = (tt, _r_payload(f, tt, depth + 1))
    raise ValueError(f"Unknown NBT tag {t}")


def _w_str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _w_payload(t, v):
    if t in _FMT:
        return struct.pack(_FMT[t], v)
    if t == STRING:
        return _w_str(v)
    if t == BYTE_ARRAY:
        return struct.pack(">i", len(v)) + bytes(v)
    if t in (INT_ARRAY, LONG_ARRAY):
        return struct.pack(">i", len(v)) + b"".join(struct.pack(">i" if t == INT_ARRAY else ">q", x) for x in v)
    if t == LIST:
        et, items = v
        return struct.pack(">bi", et if items else END, len(items)) + b"".join(_w_payload(et, x) for x in items)
    if t == COMPOUND:
        return b"".join(struct.pack(">b", tt) + _w_str(k) + _w_payload(tt, vv) for k, (tt, vv) in v.items()) + b"\x00"
    raise ValueError(f"Unknown NBT tag {t}")


def read_nbt(data):
    f = io.BytesIO(data)
    if _r(f, ">b") != COMPOUND:
        raise ValueError("servers.dat doesn't start with a compound tag")
    name = _r_str(f)
    return name, _r_payload(f, COMPOUND)


def write_nbt(name, root):
    return struct.pack(">b", COMPOUND) + _w_str(name) + _w_payload(COMPOUND, root)


# ------------------------------------------------------------ servers.dat ---
def read_server_list(game_dir):
    """[{name, ip}] from the in-game Multiplayer list."""
    p = Path(game_dir) / "servers.dat"
    if not p.exists():
        return []
    _, root = read_nbt(p.read_bytes())
    _, (_, entries) = root.get("servers", (LIST, (COMPOUND, [])))
    return [{"name": e.get("name", (STRING, ""))[1], "ip": e.get("ip", (STRING, ""))[1]} for e in entries]


def add_to_server_list(game_dir, name, address):
    """Put this server at the top of the in-game Multiplayer list, keeping every
    other entry. If it's already there it moves to the top (keeping its icon).
    Returns True if the file changed."""
    p = Path(game_dir) / "servers.dat"
    root_name, root = "", {}
    if p.exists():
        root_name, root = read_nbt(p.read_bytes())
    _, (et, entries) = root.get("servers", (LIST, (COMPOUND, [])))
    if entries and et != COMPOUND:
        raise ValueError("servers.dat has an unexpected layout; not changing it.")

    def same(e):
        ip = e.get("ip", (STRING, ""))[1].strip().lower()
        return ip == address.strip().lower()
    existing = [e for e in entries if same(e)]
    others = [e for e in entries if not same(e)]
    if (len(existing) == 1 and entries[0] is existing[0] and "hidden" not in existing[0]
            and existing[0].get("name", (STRING, ""))[1] == name):
        return False  # already first in the list
    entry = existing[0] if existing else {}
    entry["name"] = (STRING, name)
    entry["ip"] = (STRING, address)
    entry.pop("hidden", None)
    new_entries = [entry] + others
    root["servers"] = (LIST, (COMPOUND, new_entries))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("servers.dat.vibe-tmp")
    tmp.write_bytes(write_nbt(root_name, root))
    os.replace(tmp, p)
    return True


def favicon_bytes(data_uri):
    """Decode a cleaned favicon data URI (used by tests)."""
    return base64.b64decode(data_uri.split(",", 1)[1]) if data_uri else b""
