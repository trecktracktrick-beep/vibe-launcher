"""Microsoft sign-in with a device code, the way Prism and other standalone
launchers do it: Vibe shows a short code, you enter it at microsoft.com/link
(in any browser, even on your phone), and Vibe finishes the sign-in itself.

Then: Microsoft token -> Xbox Live -> XSTS -> Minecraft token -> profile.
Needs your own Azure app (Client ID) with "Allow public client flows" on and
approval for the Minecraft API. Tokens are never logged.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPE = "XboxLive.signin offline_access"
UA = "VibeLauncher"


class AuthError(Exception):
    """A sign-in problem with a plain-language message."""

    def __init__(self, msg, code=""):
        super().__init__(msg)
        self.code = code


class Pending(Exception):
    """The user hasn't entered the code yet."""


class SlowDown(Exception):
    pass


XERR = {
    2148916233: "This Microsoft account has no Xbox profile yet. Sign in once at xbox.com to create one, then try again.",
    2148916235: "Xbox Live isn't available in your country or region.",
    2148916236: "This account needs adult verification on xbox.com (South Korea).",
    2148916237: "This account needs adult verification on xbox.com (South Korea).",
    2148916238: "This is a child account. An adult needs to add it to a Microsoft family group first.",
}


def _post(url, data=None, json_body=None, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    else:
        body = urllib.parse.urlencode(data or {}).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    return _send(req, timeout)


def _get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    return _send(req, timeout)


def _send(req, timeout):
    """Returns (status, json). HTTP errors are returned, not raised; the
    callers turn them into friendly messages."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AuthError("Couldn't reach Microsoft. Check your internet connection.", "network") from e
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {}


# ------------------------------------------------------------ device code ---
def start_device_code(client_id):
    """-> {user_code, verification_uri, device_code, expires_in, interval}"""
    if not client_id:
        raise AuthError("Add your Azure Client ID in Settings first.", "no_client_id")
    st, j = _post(f"{AUTHORITY}/devicecode", {"client_id": client_id, "scope": SCOPE})
    if st != 200 or "device_code" not in j:
        desc = j.get("error_description", "") or ""
        if "AADSTS7000218" in desc or "public client" in desc.lower():
            raise AuthError("Turn on 'Allow public client flows' in your Azure app (Authentication page), "
                            "then try again.", "public_client")
        if "AADSTS700016" in desc or j.get("error") == "unauthorized_client":
            raise AuthError("Microsoft doesn't recognise this Client ID. Check it in Settings, and that the app "
                            "allows personal Microsoft accounts.", "bad_client_id")
        raise AuthError(f"Microsoft refused the sign-in request ({j.get('error', st)}).", "devicecode")
    return {k: j.get(k) for k in ("user_code", "verification_uri", "device_code", "expires_in", "interval")}


def poll_device_code(client_id, device_code):
    """One poll. Returns the Microsoft token response, or raises Pending /
    SlowDown / AuthError."""
    st, j = _post(f"{AUTHORITY}/token", {"client_id": client_id, "device_code": device_code,
                                         "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if st == 200 and "access_token" in j:
        return j
    err = j.get("error", "")
    if err == "authorization_pending":
        raise Pending()
    if err == "slow_down":
        raise SlowDown()
    if err in ("authorization_declined", "access_denied"):
        raise AuthError("Sign-in was cancelled.", "declined")
    if err in ("expired_token", "code_expired"):
        raise AuthError("The code expired. Start the sign-in again.", "expired")
    raise AuthError(f"Sign-in failed ({err or st}).", err or "token")


def refresh_ms_token(client_id, refresh_token):
    st, j = _post(f"{AUTHORITY}/token", {"client_id": client_id, "scope": SCOPE,
                                         "refresh_token": refresh_token, "grant_type": "refresh_token"})
    if st != 200 or "access_token" not in j:
        raise AuthError("Your sign-in expired. Please sign in again.", "refresh")
    return j


# --------------------------------------------------- xbox -> minecraft ---
def minecraft_login(ms_token):
    """Microsoft token response -> {id, name, access_token, refresh_token, skins, capes}"""
    st, xbl = _post("https://user.auth.xboxlive.com/user/authenticate", json_body={
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": f"d={ms_token['access_token']}"},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
    if st != 200 or "Token" not in xbl:
        raise AuthError("Xbox Live sign-in failed. Try again in a minute.", "xbl")
    uhs = xbl["DisplayClaims"]["xui"][0]["uhs"]

    st, xsts = _post("https://xsts.auth.xboxlive.com/xsts/authorize", json_body={
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
    if st != 200 or "Token" not in xsts:
        raise AuthError(XERR.get(xsts.get("XErr"), f"Xbox Live refused the sign-in ({xsts.get('XErr', st)})."), "xsts")

    st, mc = _post("https://api.minecraftservices.com/authentication/login_with_xbox",
                   json_body={"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
    if st != 200 or "access_token" not in mc:
        raise AuthError("Your Azure app isn't approved for the Minecraft API yet. Apply at "
                        "aka.ms/mce-reviewappid; until then you can use the official launcher route or the demo.",
                        "AzureAppNotPermitted")

    st, prof = _get("https://api.minecraftservices.com/minecraft/profile",
                    headers={"Authorization": f"Bearer {mc['access_token']}"})
    if st == 404 or prof.get("error") == "NOT_FOUND" or "id" not in prof:
        raise AuthError("This Microsoft account doesn't own Minecraft: Java Edition.", "AccountNotOwnMinecraft")
    return {"id": prof["id"], "name": prof["name"], "skins": prof.get("skins", []), "capes": prof.get("capes", []),
            "access_token": mc["access_token"], "refresh_token": ms_token.get("refresh_token", "")}


def refresh(client_id, refresh_token):
    return minecraft_login(refresh_ms_token(client_id, refresh_token))
