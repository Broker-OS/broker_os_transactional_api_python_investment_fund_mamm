"""
Filtro de logging que redacta secretos antes de que lleguen a un handler.

Guia PAMM §2: "La CRM API key debe permanecer exclusivamente en servidores
backend. No debe enviarse al navegador, aplicaciones moviles, repositorios,
**logs** ni herramientas de analitica."
Guia PAMM §6.4/§8.2: "No llame este endpoint desde el navegador ni almacene la
respuesta completa en logs. Trate ambas contrasenas como secretos."

Es una red de seguridad, no una excusa para loguear payloads: el codigo sigue
sin loguear bodies de respuesta. Esto ataja lo que se escape por un traceback,
un `repr()` de excepcion de httpx o un log de debug agregado a futuro.
"""
from __future__ import annotations

import logging
import re
from typing import Any

MASK = "***REDACTED***"

# Claves cuyo valor nunca debe aparecer en un log, en formato JSON o query string.
_SECRET_KEYS = (
    "mt5_password",
    "mt5_investor_password",
    "investor_password",
    "new_password",
    "password",
    "api_key",
    "apikey",
    "crm_api_key",
    "authorization",
    "x-api-key",
)

_KEY_ALT = "|".join(re.escape(k) for k in _SECRET_KEYS)

# "clave": "valor"  /  'clave': 'valor'  (JSON o dict de Python)
_JSON_RE = re.compile(
    rf"""(?P<prefix>["']?(?:{_KEY_ALT})["']?\s*[:=]\s*)(?P<quote>["'])(?P<value>(?:\\.|[^"'\\])*)(?P=quote)""",
    re.IGNORECASE,
)
# clave=valor en query string o texto plano
_QS_RE = re.compile(rf"(?P<prefix>\b(?:{_KEY_ALT})=)(?P<value>[^&\s,;)}}\"']+)", re.IGNORECASE)
# Bearer <token>
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._\-]+)")


def redact(text: str) -> str:
    """Devuelve el texto con todos los secretos conocidos enmascarados."""
    if not text:
        return text
    out = _JSON_RE.sub(lambda m: f"{m.group('prefix')}{m.group('quote')}{MASK}{m.group('quote')}", text)
    out = _QS_RE.sub(lambda m: f"{m.group('prefix')}{MASK}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}{MASK}", out)

    # La CRM API key puede aparecer suelta (sin clave que la preceda).
    from app.core.config import settings  # import diferido: evita ciclo en el arranque

    for secret in (settings.CODENCH_CRM_API_KEY, settings.MT5_CREDENTIALS_ENCRYPTION_KEY):
        if secret and len(secret) >= 8 and secret in out:
            out = out.replace(secret, MASK)
    return out


def _redact_any(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


class SecretRedactionFilter(logging.Filter):
    """Aplica `redact` al mensaje y a los argumentos de cada LogRecord.

    Nunca descarta registros ni deja propagar una excepcion: si la redaccion
    falla, es preferible perder el enmascarado que perder el log... salvo el
    mensaje mismo, que en ese caso se reemplaza por completo.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _redact_any(v) for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(_redact_any(a) for a in record.args)
        except Exception:  # pragma: no cover - defensivo
            record.msg = "[log redaction failed; mensaje suprimido por seguridad]"
            record.args = None
        return True


# Loggers que emiten con handlers propios (no cuelgan del root): sus registros
# nunca pasarian por el filtro si solo lo instalaramos en los handlers del root.
_NAMED_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore")


def _add(target: logging.Logger | logging.Handler, f: logging.Filter) -> None:
    if not any(isinstance(x, SecretRedactionFilter) for x in target.filters):
        target.addFilter(f)


def install(root: logging.Logger | None = None) -> None:
    """Instala el filtro en los handlers del root y en los loggers con handler propio.

    Los filtros de un *logger* solo se aplican a los registros emitidos por ese
    logger (no a los que suben desde hijos); los de un *handler* se aplican a
    todo lo que pase por el. Por eso se cubren ambos.
    """
    logger = root or logging.getLogger()
    f = SecretRedactionFilter()
    for handler in logger.handlers:
        _add(handler, f)
    for name in _NAMED_LOGGERS:
        _add(logging.getLogger(name), f)
