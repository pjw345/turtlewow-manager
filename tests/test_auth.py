import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch
from auth import Auth, password_hash, create_admin

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / 'account.json'
        self.salt = 'a1' * 32
        self.path.write_text(json.dumps({'username': 'admin', 'salt': self.salt, 'hash': password_hash('test-password-only', self.salt)}))
        self.auth = Auth(self.path)
    def tearDown(self):
        self.temp.cleanup()
    def test_wrong_password_and_username_rejected(self):
        self.assertIsNone(self.auth.login('admin', 'wrong')[0])
        self.assertIsNone(self.auth.login('other', 'test-password-only')[0])
    def test_short_password_still_requests_confirmation(self):
        with patch('builtins.input', return_value='admin'), patch('auth.getpass.getpass', side_effect=['short', 'short']) as prompt:
            with self.assertRaisesRegex(RuntimeError, 'too short'):
                create_admin(self.path)
            self.assertEqual(prompt.call_count, 2)
    def test_login_expiry_logout_and_password_change(self):
        token, error = self.auth.login('admin', 'test-password-only')
        self.assertIsNone(error)
        self.assertTrue(self.auth.session('turtlewow_session=' + token)[1]['csrf'])
        self.auth.sessions[token]['last'] = time.monotonic() - 1801
        self.assertIsNone(self.auth.session('turtlewow_session=' + token)[1])
        token, _ = self.auth.login('admin', 'test-password-only')
        self.auth.logout(token)
        self.assertIsNone(self.auth.session('turtlewow_session=' + token)[1])
        token, _ = self.auth.login('admin', 'test-password-only')
        self.path.write_text(json.dumps({'hash': 'changed'}))
        self.assertIsNone(self.auth.session('turtlewow_session=' + token)[1])
    def test_rate_limit_and_missing_account(self):
        for _ in range(8):
            self.auth.login('admin', 'wrong')
        self.assertIn('Too many', self.auth.login('admin', 'test-password-only')[1])
        self.path.unlink()
        self.auth.attempts.clear()
        self.assertIsNone(self.auth.login('admin', 'test-password-only')[0])
