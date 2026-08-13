"""
Emision de alertas operativas.

Checklist de salida a produccion de la guia PAMM: "Alertas sobre errores 500/503
y discrepancias entre CRM, PAMM y MT5".

Las alertas salen por un logger dedicado (`app.alerts`) en nivel ERROR y con un
prefijo parseable, para que cualquier colector (CloudWatch, Loki, Datadog) las
filtre con una sola regla:

    [ALERT:UNCERTAIN_RESULT] Deposito con resultado incierto | movement_id=... login=...

No se manda mail desde aca a proposito: el envio real es una decision de
infraestructura y un mail por cada 5xx del proveedor seria ruido. El contrato es
el prefijo; engancharlo a un canal es configuracion del colector.

Ojo: `app.alerts` propaga al logger raiz, asi que le aplica el filtro de
redaccion de secretos. Aun asi, NUNCA pasar contrasenas ni la API key como
contexto.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.alerts")

# Errores de comunicacion con el proveedor.
PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"      # 500/503
UNCERTAIN_RESULT = "UNCERTAIN_RESULT"                # timeout en operacion no idempotente

# Discrepancias CRM <-> PAMM <-> MT5.
UNRECONCILED_MOVEMENT = "UNRECONCILED_MOVEMENT"      # movimiento AMBIGUOUS
UNATTRIBUTED_PERF_FEE = "UNATTRIBUTED_PERF_FEE"      # fee que no se pudo imputar
NEGATIVE_CAPITAL = "NEGATIVE_CAPITAL"                # fees > capital atribuido
LEDGER_INCONSISTENT = "LEDGER_INCONSISTENT"          # el ledger no pudo cerrar
ORPHAN_MT5_ACCOUNT = "ORPHAN_MT5_ACCOUNT"            # cuenta creada en el broker, no guardada
STALE_SUBSCRIPTION = "STALE_SUBSCRIPTION"            # PAUSED/PENDING sin resolverse

_SAFE = (str, int, float, bool, type(None))


def _fmt(context: dict[str, Any]) -> str:
    parts = []
    for k, v in context.items():
        parts.append(f"{k}={v if isinstance(v, _SAFE) else type(v).__name__}")
    return " ".join(parts)


def alert(kind: str, message: str, **context: Any) -> None:
    """Emite una alerta operativa. `kind` es una de las constantes de este modulo."""
    suffix = _fmt(context)
    logger.error("[ALERT:%s] %s%s", kind, message, f" | {suffix}" if suffix else "")
