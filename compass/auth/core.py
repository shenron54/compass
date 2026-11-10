import sqlite3

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "compass_session"
SESSION_MAX_AGE = 7 * 24 * 3600
MAX_FAILURES_PER_IP = 5
MAX_FAILURES_TOTAL = 20
WINDOW_MINUTES = 15

_hasher = PasswordHasher()
# Computed once so a wrong username costs the same time as a wrong password.
_DUMMY_HASH = _hasher.hash("compass-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    code = code.strip().replace(" ", "")
    return code.isdigit() and pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="compass")


def create_user(conn: sqlite3.Connection, username: str, password: str,
                secret: str | None = None) -> str:
    if len(password) < 12:
        raise ValueError("use a password of at least 12 characters")
    secret = secret or new_totp_secret()
    conn.execute(
        "INSERT INTO users(username, password_hash, totp_secret) VALUES (?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, "
        "totp_secret = excluded.totp_secret, session_version = users.session_version + 1",
        (username, hash_password(password), secret))
    return secret


def too_many_failures(conn: sqlite3.Connection, ip: str) -> bool:
    window = f"-{WINDOW_MINUTES} minutes"
    per_ip = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ok = 0 AND ip = ? AND ts >= datetime('now', ?)",
        (ip, window)).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ok = 0 AND ts >= datetime('now', ?)",
        (window,)).fetchone()[0]
    return per_ip >= MAX_FAILURES_PER_IP or total >= MAX_FAILURES_TOTAL


def authenticate(conn: sqlite3.Connection, username: str, password: str, code: str,
                 ip: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    ok = verify_password(row["password_hash"] if row else _DUMMY_HASH, password)
    ok = ok and row is not None and verify_totp(row["totp_secret"], code)
    conn.execute("INSERT INTO login_attempts(ip, ok) VALUES (?, ?)", (ip, int(ok)))
    conn.execute("DELETE FROM login_attempts WHERE ts < datetime('now', '-30 days')")
    return row if ok else None


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="compass-session")


def make_session(secret: str, user: sqlite3.Row) -> str:
    return _serializer(secret).dumps({"uid": user["id"], "sv": user["session_version"]})


def read_session(conn: sqlite3.Connection, secret: str, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    try:
        data = _serializer(secret).loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    row = conn.execute("SELECT * FROM users WHERE id = ?", (data.get("uid"),)).fetchone()
    if row is None or row["session_version"] != data.get("sv"):
        return None
    return row


def revoke_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?",
                 (user_id,))
