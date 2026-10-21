"""Passwords and tokens, using only the standard library.

* Passwords: scrypt with a random salt per user; the stored string records its parameters so
  they can be raised later. Compared in constant time.
* Tokens (login sessions and share links): 256 random bits, shown to the user ONCE. Only a
  SHA-256 hash is stored, so a copy of the database cannot be used to log in or open a share.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from whymoved.app.errors import Invalid

_N, _R, _P = 2**14, 8, 1
MIN_PASSWORD, MAX_PASSWORD = 10, 200
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise Invalid("enter a valid email address")
    return email


def check_password_rules(password: str, email: str = "") -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise Invalid(f"password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise Invalid(f"password must be at most {MAX_PASSWORD} characters")
    if email and password.lower() == email.lower():
        raise Invalid("password must not be the same as the email")
    if len(set(password)) < 4:
        raise Invalid("password is too repetitive")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                             r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# Hash of a throwaway password: verified against when the email is unknown, so a login for an
# unknown email takes about as long as one for a known email.
DUMMY_HASH = hash_password("not-a-real-password")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id() -> str:
    return secrets.token_hex(8)
