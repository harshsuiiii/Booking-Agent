"""
Password hashing with scrypt (Python standard library, memory-hard).

Stored format: scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>
"""

import base64
import hashlib
import hmac
import secrets

_N, _R, _P = 2**14, 8, 1
_DKLEN = 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "$".join(["scrypt", str(_N), str(_R), str(_P), _b64(salt), _b64(digest)])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the email is unknown, so a miss costs the same time as a hit.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
