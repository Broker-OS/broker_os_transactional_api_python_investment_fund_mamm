"""
Cifrado en reposo de las credenciales MT5 (Fernet / AES-128-CBC + HMAC).

Checklist de salida a produccion de la guia PAMM: "Persistencia cifrada para
credenciales MT5". Las contrasenas que devuelven /master/create, /investor/create
y los endpoints /info/* son secretos y NUNCA deben quedar en claro en la BD.

Falla CERRADO a proposito: si no hay clave configurada, `encrypt` levanta en vez
de guardar en claro. Preferimos que el provisioning falle ruidosamente antes que
persistir una contrasena MT5 sin cifrar.

Generar una clave:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.exceptions import Mt5CredentialsEncryptionError

logger = logging.getLogger(__name__)

_fernet: Optional[Fernet] = None
_loaded = False


def _get_fernet() -> Optional[Fernet]:
    """Instancia perezosa. None si no hay clave configurada."""
    global _fernet, _loaded
    if _loaded:
        return _fernet
    _loaded = True
    key = (settings.MT5_CREDENTIALS_ENCRYPTION_KEY or "").strip()
    if not key:
        logger.warning(
            "MT5_CREDENTIALS_ENCRYPTION_KEY no configurada: el provisioning de "
            "cuentas MT5 quedara deshabilitado (no se guardan credenciales en claro)."
        )
        return None
    try:
        _fernet = Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        # No incluir la clave en el mensaje.
        raise Mt5CredentialsEncryptionError(
            message="La clave de cifrado de credenciales MT5 es invalida",
            detail=f"MT5_CREDENTIALS_ENCRYPTION_KEY no es una clave Fernet valida: {type(exc).__name__}",
        ) from exc
    return _fernet


def is_configured() -> bool:
    return _get_fernet() is not None


def encrypt(plaintext: Optional[str]) -> Optional[bytes]:
    """Cifra un secreto. Devuelve None si la entrada es None/vacia."""
    if plaintext is None or plaintext == "":
        return None
    f = _get_fernet()
    if f is None:
        raise Mt5CredentialsEncryptionError(
            message="No se pueden guardar credenciales MT5: falta configurar el cifrado",
            detail="Definir MT5_CREDENTIALS_ENCRYPTION_KEY en .env",
        )
    return f.encrypt(plaintext.encode())


def decrypt(token: Optional[bytes]) -> Optional[str]:
    """Descifra un secreto. Devuelve None si no hay valor almacenado."""
    if token is None:
        return None
    f = _get_fernet()
    if f is None:
        raise Mt5CredentialsEncryptionError(
            message="No se pueden leer credenciales MT5: falta configurar el cifrado",
            detail="Definir MT5_CREDENTIALS_ENCRYPTION_KEY en .env",
        )
    try:
        return f.decrypt(token).decode()
    except InvalidToken as exc:
        raise Mt5CredentialsEncryptionError(
            message="No se pudo descifrar la credencial MT5 almacenada",
            detail="Token invalido: la clave de cifrado cambio o el dato esta corrupto",
        ) from exc


def reset_cache() -> None:
    """Solo para tests: fuerza releer la clave de settings."""
    global _fernet, _loaded
    _fernet, _loaded = None, False
