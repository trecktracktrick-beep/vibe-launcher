"""Tests for the Servers feature. No internet needed: the ping test talks to a
tiny fake Minecraft server on localhost."""
import io
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vibe_core import servers as sv  # noqa: E402


class AddressTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sv.parse_address("Play.Example.net"), ("play.example.net", None))
        self.assertEqual(sv.parse_address("play.example.net:25570"), ("play.example.net", 25570))
        self.assertEqual(sv.parse_address("1.2.3.4"), ("1.2.3.4", None))
        self.assertEqual(sv.parse_address("[::1]:25565"), ("::1", 25565))

    def test_rejects_bad(self):
        for bad in ("", "a b", "x:99999", "x:abc", "-bad-.com", "http://x.com", "a" * 300):
            with self.assertRaises(ValueError, msg=bad):
                sv.parse_address(bad)

    def test_format(self):
        self.assertEqual(sv.format_address("a.net", 25565), "a.net")
        self.assertEqual(sv.format_address("a.net", None), "a.net")
        self.assertEqual(sv.format_address("a.net", 25570), "a.net:25570")
        self.assertEqual(sv.format_address("::1", 25570), "[::1]:25570")

    def test_suggested_are_valid(self):
        ids = [s["id"] for s in sv.SUGGESTED]
        self.assertEqual(len(ids), len(set(ids)))
        for s in sv.SUGGESTED:
            sv.parse_address(s["address"])

    def test_nslookup_windows(self):
        out = ("_minecraft._tcp.x.net\tSRV service location:\n\t  priority       = 0\n"
               "\t  weight         = 5\n\t  port           = 25577\n\t  svr hostname   = mc.x.net\n")
        self.assertEqual(sv.parse_nslookup_srv(out), ("mc.x.net", 25577))

    def test_nslookup_unix(self):
        out = "_minecraft._tcp.x.net\tservice = 0 5 25578 mc.x.net.\n"
        self.assertEqual(sv.parse_nslookup_srv(out), ("mc.x.net", 25578))
        self.assertIsNone(sv.parse_nslookup_srv("** server can't find _minecraft._tcp.x.net: NXDOMAIN"))


class StatusTests(unittest.TestCase):
    def test_chat_to_text(self):
        self.assertEqual(sv.chat_to_text("§aHello §lworld"), "Hello world")
        self.assertEqual(sv.chat_to_text({"text": "A", "extra": [{"text": "B"}, "§cC"]}), "ABC")
        self.assertEqual(sv.chat_to_text([{"text": "x"}, "y"]), "xy")
        self.assertEqual(sv.chat_to_text(None), "")

    def test_favicon_only_png_base64(self):
        good = "data:image/png;base64,iVBORw0KGgo="
        self.assertEqual(sv._clean_favicon(good), good)
        self.assertEqual(sv._clean_favicon('data:image/png;base64,AAA" onerror="x'), "")
        self.assertEqual(sv._clean_favicon("data:text/html;base64,AAAA"), "")
        self.assertEqual(sv._clean_favicon(123), "")

    def test_parse_status(self):
        r = sv.parse_status(json.dumps({"description": {"text": "§6Survival"}, "players": {"online": 5, "max": 50,
                            "sample": [{"name": "§aSteve", "id": "x"}]}, "version": {"name": "26.2", "protocol": 800}}))
        self.assertEqual((r["motd"], r["online_players"], r["max_players"], r["version"]), ("Survival", 5, 50, "26.2"))
        self.assertEqual(r["sample"], ["Steve"])


def _fake_server(status, answer_ping=True):
    """Minimal Minecraft status responder on a random localhost port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen = {}

    def run():
        conn, _ = srv.accept()
        with conn:
            f = conn.makefile("rb")
            read = f.read
            for _ in range(2):  # handshake + status request
                n = sv._read_varint(read)
                body = io.BytesIO(read(n))
                pid = sv._read_varint(body.read)
                if pid == 0 and n > 1:
                    seen["protocol"] = sv._read_varint(body.read)
                    seen["host"] = body.read(sv._read_varint(body.read)).decode()
                    seen["port"] = struct.unpack(">H", body.read(2))[0]
                    seen["next"] = sv._read_varint(body.read)
            conn.sendall(sv._packet(0x00, sv._string(json.dumps(status))))
            if answer_ping:
                n = sv._read_varint(read)
                payload = read(n)
                conn.sendall(sv._varint(len(payload)) + payload)
        srv.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return port, seen, t


class PingTests(unittest.TestCase):
    STATUS = {"description": "§aA §lfriendly§r survival server", "players": {"online": 12, "max": 100},
              "version": {"name": "26.2", "protocol": 800}, "favicon": "data:image/png;base64,iVBORw0KGgo="}

    def test_ping_round_trip(self):
        port, seen, t = _fake_server(self.STATUS)
        r = sv.ping(f"127.0.0.1:{port}", timeout=3)
        t.join(3)
        self.assertTrue(r["online"], r)
        self.assertEqual(r["online_players"], 12)
        self.assertEqual(r["motd"], "A friendly survival server")
        self.assertEqual(r["favicon"], self.STATUS["favicon"])
        self.assertGreaterEqual(r["ping_ms"], 0)
        self.assertEqual((seen["host"], seen["port"], seen["next"], seen["protocol"]), ("127.0.0.1", port, 1, -1))

    def test_ping_without_pong(self):
        port, _, t = _fake_server(self.STATUS, answer_ping=False)
        r = sv.ping(f"127.0.0.1:{port}", timeout=3)
        t.join(3)
        self.assertTrue(r["online"])

    def test_offline(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listening there now
        r = sv.ping(f"127.0.0.1:{port}", timeout=2)
        self.assertFalse(r["online"])
        self.assertTrue(r["error"])

    def test_bad_address_never_raises(self):
        self.assertFalse(sv.ping("not a server")["online"])


class ServersDatTests(unittest.TestCase):
    def _write(self, d, entries, extra_root=None):
        root = {"servers": (sv.LIST, (sv.COMPOUND, entries))}
        root.update(extra_root or {})
        (Path(d) / "servers.dat").write_bytes(sv.write_nbt("", root))

    def test_nbt_round_trip_all_types(self):
        root = {"b": (sv.BYTE, -3), "s": (sv.SHORT, 300), "i": (sv.INT, 70000), "l": (sv.LONG, 2**40),
                "f": (sv.FLOAT, 1.5), "d": (sv.DOUBLE, 2.25), "ba": (sv.BYTE_ARRAY, b"\x01\x02"),
                "str": (sv.STRING, "héllo"), "ia": (sv.INT_ARRAY, [1, -2]), "la": (sv.LONG_ARRAY, [2**33]),
                "lst": (sv.LIST, (sv.STRING, ["a", "b"])), "empty": (sv.LIST, (sv.END, [])),
                "c": (sv.COMPOUND, {"x": (sv.INT, 1)})}
        data = sv.write_nbt("root", root)
        self.assertEqual(sv.read_nbt(data), ("root", root))
        self.assertEqual(sv.write_nbt(*sv.read_nbt(data)), data)

    def test_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(sv.add_to_server_list(d, "Vanilla Realms", "play.vanillarealms.com"))
            self.assertEqual(sv.read_server_list(d), [{"name": "Vanilla Realms", "ip": "play.vanillarealms.com"}])

    def test_keeps_others_and_their_fields(self):
        with tempfile.TemporaryDirectory() as d:
            mine = {"name": (sv.STRING, "My SMP"), "ip": (sv.STRING, "smp.friend.net"),
                    "icon": (sv.STRING, "AAAA"), "acceptTextures": (sv.BYTE, 1)}
            self._write(d, [mine], {"custom": (sv.INT, 7)})
            sv.add_to_server_list(d, "New", "new.example.net")
            _, root = sv.read_nbt((Path(d) / "servers.dat").read_bytes())
            entries = root["servers"][1][1]
            self.assertEqual([e["ip"][1] for e in entries], ["new.example.net", "smp.friend.net"])
            self.assertEqual(entries[1], mine)
            self.assertEqual(root["custom"], (sv.INT, 7))

    def test_moves_existing_to_top_without_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, [{"name": (sv.STRING, "A"), "ip": (sv.STRING, "a.net")},
                            {"name": (sv.STRING, "B"), "ip": (sv.STRING, "B.net"), "icon": (sv.STRING, "ICON")}])
            self.assertTrue(sv.add_to_server_list(d, "B", "b.net"))
            _, root = sv.read_nbt((Path(d) / "servers.dat").read_bytes())
            entries = root["servers"][1][1]
            self.assertEqual([e["ip"][1] for e in entries], ["b.net", "a.net"])
            self.assertEqual(entries[0]["icon"][1], "ICON")  # icon kept
            self.assertFalse(sv.add_to_server_list(d, "B", "b.net"))  # already on top: no rewrite

    def test_refuses_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "servers.dat").write_bytes(b"\x1f\x8bnot nbt")
            with self.assertRaises(ValueError):
                sv.add_to_server_list(d, "X", "x.net")
            self.assertEqual((Path(d) / "servers.dat").read_bytes(), b"\x1f\x8bnot nbt")


if __name__ == "__main__":
    unittest.main()
