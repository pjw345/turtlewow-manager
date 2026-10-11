"""Single administrator login; credentials stay outside the repository."""
import getpass
import hashlib
import hmac
import json
import pathlib
import secrets
import threading
import time
from http.cookies import SimpleCookie, CookieError

ITERATIONS = 600000


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def create_admin(path):
    username = input('Dashboard username [admin]: ').strip() or 'admin'
    password = getpass.getpass('New dashboard password (at least 12 characters): ')
    confirmation = getpass.getpass('Confirm password: ')
    if len(password) < 12:
        raise RuntimeError('Password is too short. Use at least 12 characters and run create-admin again.')
    if password != confirmation:
        raise RuntimeError('Passwords do not match. Run create-admin again.')
    salt = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.chmod(0o600)
    temporary.write_text(json.dumps({'username': username, 'salt': salt, 'hash': password_hash(password, salt)}))
    temporary.replace(path)
    print('Dashboard account saved. Restart the manager to invalidate existing sessions.')


class Auth:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.sessions = {}
        self.attempts = []
        self.lock = threading.Lock()

    def login(self, username, password):
        with self.lock:
            now = time.monotonic()
            self.attempts = [stamp for stamp in self.attempts if now - stamp < 300]
            if len(self.attempts) >= 8:
                return None, 'Too many attempts. Wait five minutes.'
            self.attempts.append(now)
            if not self.path.exists():
                return None, 'Create the dashboard account in the Ubuntu terminal first.'
            account = json.loads(self.path.read_text())
            candidate = password_hash(password, account['salt'])
            if not hmac.compare_digest(candidate, account['hash']) or not hmac.compare_digest(username, account['username']):
                return None, 'Invalid username or password.'
            self.attempts.clear()
            self.sessions = {key: value for key, value in self.sessions.items() if now < value['expires']}
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {'csrf': secrets.token_urlsafe(32), 'expires': now + 28800,
                                    'last': now, 'username': username, 'hash': account['hash']}
            return token, None

    def session(self, cookie):
        try:
            parsed = SimpleCookie(cookie or '')
            token = parsed['turtlewow_session'].value if 'turtlewow_session' in parsed else ''
        except CookieError:
            return None, None
        with self.lock:
            session = self.sessions.get(token)
            now = time.monotonic()
            account = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not session or now >= session['expires'] or now - session['last'] > 1800 or session['hash'] != account.get('hash'):
                self.sessions.pop(token, None)
                return None, None
            session['last'] = now
            return token, dict(session)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
