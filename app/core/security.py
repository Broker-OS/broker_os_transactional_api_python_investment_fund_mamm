"""Generacion y verificacion de API keys (Argon2).

La key se muestra UNA sola vez al crearla; en BD solo vive su hash Argon2 + un
prefijo publico corto (para localizar el hash sin exponer la key)."""
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()

_KEY_PREFIX = "csb_"       # Bridge Markets social bridge
_STORED_PREFIX_LEN = 12    # cuanto del inicio de la key se guarda como pista


def generate_api_key() -> tuple[str, str, str]:
    """Devuelve (full_key, key_prefix, key_hash). `full_key` se entrega una sola vez."""
    full_key = _KEY_PREFIX + secrets.token_urlsafe(32)
    key_prefix = full_key[:_STORED_PREFIX_LEN]
    key_hash = _ph.hash(full_key)
    return full_key, key_prefix, key_hash


def key_prefix_of(presented: str) -> str:
    return presented[:_STORED_PREFIX_LEN]


def verify_api_key(presented: str, key_hash: str) -> bool:
    try:
        return _ph.verify(key_hash, presented)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# ── OTP: hash/verify del codigo (mismo hasher Argon2) ──
def hash_code(code: str) -> str:
    return _ph.hash(code)


def verify_code(code: str, code_hash: str) -> bool:
    try:
        return _ph.verify(code_hash, code)
    except VerifyMismatchError:
        return False
    except Exception:
        return False
