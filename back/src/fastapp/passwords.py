"""Django-compatible password hashing + validation, without Django at runtime.

The sidecar creates technician logins (``/technicians``) and resets their
passwords, so it must write password hashes Django's ``check_password`` accepts
and reproduce ``validate_password``'s error messages byte-for-byte — all while
staying Django-free (no ``django.setup``/settings, mirroring every other
fastapp router).

* Hashing mirrors ``PBKDF2PasswordHasher`` (Django's default): the wire format
  ``pbkdf2_sha256$<iterations>$<salt>$<b64>`` with 600000 iterations and a
  12-char ``[A-Za-z0-9]`` salt, computed with stdlib ``hashlib.pbkdf2_hmac``.
* Validation mirrors ``AUTH_PASSWORD_VALIDATORS`` (base.py): MinimumLength(8),
  CommonPassword, NumericPassword — in that order. UserAttributeSimilarity is
  a no-op here because the Django router calls ``validate_password`` with no
  ``user`` argument. The common-password set is read directly from Django's
  bundled ``common-passwords.txt.gz`` data file (a plain gzip read — never
  touches Django settings), so the "too common" check matches exactly.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import secrets
import string
from functools import lru_cache
from pathlib import Path

# PBKDF2PasswordHasher defaults (django.contrib.auth.hashers).
_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600000
_DIGEST = "sha256"

# django.utils.crypto.RANDOM_STRING_CHARS
_SALT_CHARS = string.ascii_letters + string.digits

# MinimumLengthValidator default.
_MIN_LENGTH = 8


def make_password(raw_password: str) -> str:
    """Return a Django-compatible ``pbkdf2_sha256`` hash for ``raw_password``.

    Byte-format-identical to ``PBKDF2PasswordHasher.encode`` so a hash written
    here verifies against Django's ``check_password`` (and vice-versa)."""
    salt = "".join(secrets.choice(_SALT_CHARS) for _ in range(12))
    digest = hashlib.pbkdf2_hmac(
        _DIGEST, raw_password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS
    )
    b64 = base64.b64encode(digest).decode("ascii").strip()
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${b64}"


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    """Django's bundled common-password list (lower-cased, stripped). Read
    straight from the gzip data file — no Django settings involved."""
    import django

    path = Path(django.__file__).parent / "contrib" / "auth" / "common-passwords.txt.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return frozenset(line.strip() for line in fh)


def validate_password(password: str) -> list[str]:
    """Reproduce ``django...validate_password(password)`` (no user): return the
    list of error messages in AUTH_PASSWORD_VALIDATORS order (empty = valid).

    The caller joins these with a single space, exactly like the Django
    router's ``" ".join(e.messages)``."""
    errors: list[str] = []
    # MinimumLengthValidator (min_length=8), plural message form.
    if len(password) < _MIN_LENGTH:
        errors.append(
            "This password is too short. It must contain at least "
            f"{_MIN_LENGTH} characters."
        )
    # CommonPasswordValidator.
    if password.lower().strip() in _common_passwords():
        errors.append("This password is too common.")
    # NumericPasswordValidator.
    if password.isdigit():
        errors.append("This password is entirely numeric.")
    return errors
