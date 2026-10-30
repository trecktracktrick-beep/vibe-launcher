"""Tests for the device-code Microsoft sign-in. Offline: every HTTP call is faked."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vibe_core import msauth  # noqa: E402

XBL = {"Token": "xbl", "DisplayClaims": {"xui": [{"uhs": "hash"}]}}


def fake_http(routes):
    """routes: url-substring -> (status, json) or list of them (used in order)."""
    calls = []

    def handler(url, *a, **k):
        calls.append((url, k))
        for key, val in routes.items():
            if key in url:
                if isinstance(val, list):
                    return val.pop(0)
                return val
        raise AssertionError(f"unexpected request {url}")
    return handler, calls


class DeviceCodeTests(unittest.TestCase):
    def test_needs_client_id(self):
        with self.assertRaises(msauth.AuthError) as c:
            msauth.start_device_code("")
        self.assertEqual(c.exception.code, "no_client_id")

    def test_start_ok(self):
        h, calls = fake_http({"/devicecode": (200, {"device_code": "D", "user_code": "ABCD-1234",
                                                     "verification_uri": "https://www.microsoft.com/link",
                                                     "expires_in": 900, "interval": 5})})
        with mock.patch.object(msauth, "_post", h):
            r = msauth.start_device_code("cid")
        self.assertEqual(r["user_code"], "ABCD-1234")
        self.assertEqual(calls[0][0], msauth.AUTHORITY + "/devicecode")

    def test_public_client_hint(self):
        h, _ = fake_http({"/devicecode": (400, {"error": "invalid_client",
                                                 "error_description": "AADSTS7000218: ... client_assertion"})})
        with mock.patch.object(msauth, "_post", h), self.assertRaises(msauth.AuthError) as c:
            msauth.start_device_code("cid")
        self.assertEqual(c.exception.code, "public_client")

    def test_poll_states(self):
        for err, exc in (("authorization_pending", msauth.Pending), ("slow_down", msauth.SlowDown),
                         ("expired_token", msauth.AuthError), ("authorization_declined", msauth.AuthError)):
            h, _ = fake_http({"/token": (400, {"error": err})})
            with mock.patch.object(msauth, "_post", h), self.assertRaises(exc):
                msauth.poll_device_code("cid", "D")
        h, _ = fake_http({"/token": (200, {"access_token": "ms", "refresh_token": "r"})})
        with mock.patch.object(msauth, "_post", h):
            self.assertEqual(msauth.poll_device_code("cid", "D")["access_token"], "ms")


class MinecraftLoginTests(unittest.TestCase):
    def run_login(self, xsts=(200, {"Token": "xsts"}), mc=(200, {"access_token": "mc"}),
                  profile=(200, {"id": "uuid", "name": "Steve"})):
        post, _ = fake_http({"user.auth.xboxlive.com": (200, XBL), "xsts.auth": xsts, "login_with_xbox": mc})
        get, _ = fake_http({"/minecraft/profile": profile})
        with mock.patch.object(msauth, "_post", post), mock.patch.object(msauth, "_get", get):
            return msauth.minecraft_login({"access_token": "ms", "refresh_token": "r"})

    def test_success(self):
        a = self.run_login()
        self.assertEqual((a["id"], a["name"], a["access_token"], a["refresh_token"]), ("uuid", "Steve", "mc", "r"))

    def test_app_not_approved(self):
        with self.assertRaises(msauth.AuthError) as c:
            self.run_login(mc=(403, {"error": "Invalid app registration"}))
        self.assertEqual(c.exception.code, "AzureAppNotPermitted")

    def test_no_xbox_profile_is_explained(self):
        with self.assertRaises(msauth.AuthError) as c:
            self.run_login(xsts=(401, {"XErr": 2148916233}))
        self.assertIn("Xbox profile", str(c.exception))

    def test_does_not_own_game(self):
        with self.assertRaises(msauth.AuthError) as c:
            self.run_login(profile=(404, {"error": "NOT_FOUND"}))
        self.assertEqual(c.exception.code, "AccountNotOwnMinecraft")

    def test_refresh_failure(self):
        h, _ = fake_http({"/token": (400, {"error": "invalid_grant"})})
        with mock.patch.object(msauth, "_post", h), self.assertRaises(msauth.AuthError) as c:
            msauth.refresh("cid", "old")
        self.assertEqual(c.exception.code, "refresh")

    def test_network_error_is_friendly(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")), \
                self.assertRaises(msauth.AuthError) as c:
            msauth.start_device_code("cid")
        self.assertEqual(c.exception.code, "network")


if __name__ == "__main__":
    unittest.main()
