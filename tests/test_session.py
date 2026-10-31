"""Tests for the login/session layer against the mock box."""

from __future__ import annotations

import hashlib
import unittest

from fbsh.session import Blocked, LoginFailed, Session
from tests.mock_fritz import MockBox


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.box = MockBox(password="geheim", user="example_user").start()
        self.addCleanup(self.box.stop)

    def session(self, password="geheim", user="example_user"):
        return Session(host="127.0.0.1", port=self.box.port, password=password, user=user, timeout=5)

    def test_pbkdf2_login(self):
        session = self.session()
        sid = session.login()
        self.assertEqual(len(sid), 16)
        self.assertIn(sid, self.box.valid_sids)

    def test_md5_login_for_old_firmware(self):
        self.box.md5_only = True
        sid = self.session().login()
        self.assertIn(sid, self.box.valid_sids)

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(LoginFailed):
            self.session(password="falsch").login()

    def test_wrong_user_is_rejected(self):
        with self.assertRaises(LoginFailed):
            self.session(user="unbekannt").login()

    def test_blocktime_is_reported(self):
        self.box.blocktime = 300
        with self.assertRaises(Blocked) as caught:
            self.session(password="falsch").login()
        self.assertEqual(caught.exception.seconds, 300)

    def test_sid_is_reused(self):
        session = self.session()
        session.login()
        first = self.box.login_requests
        session.sid
        session.sid
        self.assertEqual(self.box.login_requests, first, "sid should be cached")

    def test_challenge_response_matches_reference(self):
        """Compute the PBKDF2 response independently of the client code."""
        challenge = "2$1000$0011223344556677$2000$8899aabbccddeeff"
        _, it1, salt1, it2, salt2 = challenge.split("$")
        h1 = hashlib.pbkdf2_hmac("sha256", b"geheim", bytes.fromhex(salt1), int(it1))
        h2 = hashlib.pbkdf2_hmac("sha256", h1, bytes.fromhex(salt2), int(it2))
        expected = f"{salt2}${h2.hex()}"
        self.assertEqual(Session.challenge_response(challenge, "geheim"), expected)

    def test_md5_challenge_response(self):
        challenge = "1234567890"
        digest = hashlib.md5(f"{challenge}-geheim".encode("utf-16-le")).hexdigest()
        self.assertEqual(
            Session.challenge_response(challenge, "geheim"), f"{challenge}-{digest}"
        )


if __name__ == "__main__":
    unittest.main()
