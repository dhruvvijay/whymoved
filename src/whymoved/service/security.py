"""Password hashing and tokens, using only the standard library.

* Passwords: scrypt with a random 16-byte salt per user; stored as "scrypt$n$r$p$salt$hash".
* Session and share tokens: 32 random bytes (urlsafe). Only the SHA-256 of a token is stored,
  so a copy of the database does not contain usable tokens. Tokens are high-entropy random
  values, so a fast hash is appropriate (unlike passwords).
* Comparisons use hmac.compare_digest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

_N, _R, _P = 2**14, 8, 1
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
MIN_PASSWORD, MAX_PASSWORD = 8, 128


def normalise_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL.match(email)) and len(email) <= 254


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if len(password) > MAX_PASSWORD:
        return f"password must be at most {MAX_PASSWORD} characters"
    if password.strip() == "" or password.lower() in {"password", "12345678", "qwertyui"}:
        return "password is too easy to guess"
    return None


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Used when the email is unknown, so a login takes about as long either way.
DUMMY_HASH = hash_password("not-a-real-password")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_id() -> str:
    return secrets.token_hex(16)
