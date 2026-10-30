"""FRITZ!Box session handling.

Implements the login described in AVM's "Session ID" note and used by the
AHA-HTTP interface:

    1. GET  /login_sid.lua?version=2
       -> <SessionInfo><SID>0000000000000000</SID><Challenge>...</Challenge></SessionInfo>
    2. Build the response from the challenge:
         PBKDF2 (FRITZ!OS >= 7.24):  challenge = 2$iter1$salt1$iter2$salt2
             h1 = pbkdf2_sha256(password, bytes.fromhex(salt1), iter1)
             h2 = pbkdf2_sha256(h1,       bytes.fromhex(salt2), iter2)
             response = "<salt2>$<h2 hex>"
         MD5 (older firmware):       response = "<challenge>-<md5(challenge-password)>"
                                     with the MD5 over UTF-16-LE
    3. POST /login_sid.lua  username=<user>&response=<response>
       -> <SID>...</SID>, 0000000000000000 on failure, BlockTime > 0 when blocked

The SID is then passed as a query parameter to every homeautoswitch.lua call.
"""

from __future__ import annotations

import hashlib
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

LOGIN_PATH = "/login_sid.lua"
LOGIN_PATH_V2 = "/login_sid.lua?version=2"
EMPTY_SID = "0000000000000000"


class FritzError(Exception):
    """Base class for every error this package raises."""


class LoginFailed(FritzError):
    """The box rejected the credentials."""


class Blocked(FritzError):
    """The box is blocking logins for a while (after failed attempts)."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"login blocked for {seconds} s - wait or use the web UI once")


class Session:
    """Keeps one SID and renews it when the box forgets it."""

    def __init__(
        self,
        host: str = "fritz.box",
        password: str = "",
        user: str = "",
        scheme: str = "http",
        port: int | None = None,
        verify_tls: bool = False,
        ca_file: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.user = user or ""
        self.password = password
        self.scheme = scheme
        self.port = port
        self.timeout = timeout
        self._sid: str | None = None

        if scheme == "https":
            if verify_tls:
                self._context = ssl.create_default_context(cafile=ca_file)
            else:
                self._context = ssl._create_unverified_context()
        else:
            self._context = None

    # -- url -----------------------------------------------------------------
    def base_url(self) -> str:
        netloc = self.host if self.port is None else f"{self.host}:{self.port}"
        return f"{self.scheme}://{netloc}"

    def url(self, path: str, params: dict[str, object] | None = None) -> str:
        url = f"{self.base_url()}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"
        return url

    # -- transport -----------------------------------------------------------
    def request(
        self,
        path: str,
        params: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Return (status, body). HTTP errors are returned, not raised."""
        url = self.url(path, params)
        body = None
        headers = {}
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._context) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace")
        except urllib.error.URLError as err:
            raise FritzError(f"cannot reach {self.base_url()}: {err.reason}") from err

    # -- login ---------------------------------------------------------------
    @staticmethod
    def challenge_response(challenge: str, password: str) -> str:
        challenge = (challenge or "").strip()
        if challenge.startswith("2$"):
            _, iterations_1, salt_1, iterations_2, salt_2 = challenge.split("$")
            static = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(salt_1), int(iterations_1)
            )
            dynamic = hashlib.pbkdf2_hmac(
                "sha256", static, bytes.fromhex(salt_2), int(iterations_2)
            )
            return f"{salt_2}${dynamic.hex()}"
        digest = hashlib.md5(f"{challenge}-{password}".encode("utf-16-le"))
        return f"{challenge}-{digest.hexdigest()}"

    def login(self) -> str:
        status, text = self.request(LOGIN_PATH_V2)
        if status != 200:
            raise FritzError(f"login_sid.lua returned HTTP {status}")
        info = ET.fromstring(text)
        sid = (info.findtext("SID") or "").strip()
        if sid and sid != EMPTY_SID:
            self._sid = sid
            return sid

        block = int(info.findtext("BlockTime") or 0)
        if block:
            raise Blocked(block)

        challenge = info.findtext("Challenge") or ""
        response = self.challenge_response(challenge, self.password)
        status, text = self.request(
            LOGIN_PATH, data={"username": self.user, "response": response}
        )
        info = ET.fromstring(text)
        sid = (info.findtext("SID") or "").strip()
        if sid and sid != EMPTY_SID:
            self._sid = sid
            return sid

        block = int(info.findtext("BlockTime") or 0)
        if block:
            raise Blocked(block)
        raise LoginFailed("the box rejected the user/password combination")

    @property
    def sid(self) -> str:
        if not self._sid:
            self.login()
        return self._sid  # type: ignore[return-value]

    def invalidate(self) -> None:
        self._sid = None
