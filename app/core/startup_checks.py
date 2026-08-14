"""
Validaciones de configuracion al arrancar.

Spec §3.2: "La API key debe permanecer unicamente en servidores backend".

Reporta en vez de abortar: el servicio tiene que poder levantar en desarrollo con
media configuracion. Lo que no hace es callarse — cada hallazgo sale por el log
de arranque y se expone en `/health` para que sea visible sin entrar al server.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.core.config import settings

logger = logging.getLogger(__name__)

Severity = Literal["CRITICAL", "WARNING", "INFO"]


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str


def _is_production() -> bool:
    return settings.ENVIRONMENT.strip().lower() in ("production", "prod", "produccion")


def run_checks() -> list[Finding]:
    findings: list[Finding] = []
    prod = _is_production()

    # ── transporte ──
    base_url = (settings.MAM_API_BASE_URL or "").strip().lower()
    if not base_url:
        findings.append(Finding(
            "CRITICAL" if prod else "INFO", "MAM_BASE_URL_MISSING",
            "Falta MAM_API_BASE_URL: toda la integracion con el motor MAM esta deshabilitada."))
    elif base_url.startswith("http://"):
        # Sobre HTTP la API key viaja en claro en el header Authorization de
        # cada llamada, y las respuestas de cuenta traen credenciales MT5.
        findings.append(Finding(
            "CRITICAL" if prod else "WARNING",
            "MAM_URL_NOT_HTTPS",
            "La URL del MAM API usa HTTP sin cifrar: la API key viaja en claro en cada "
            "llamada y las respuestas de cuenta incluyen credenciales MT5. Pedir el "
            "endpoint HTTPS al proveedor.",
        ))
    elif not base_url.startswith("https://"):
        findings.append(Finding(
            "WARNING", "MAM_URL_INVALID", "MAM_API_BASE_URL no parece una URL valida."))

    # ── secretos ──
    if not settings.MAM_API_KEY:
        findings.append(Finding(
            "CRITICAL" if prod else "INFO", "MAM_API_KEY_MISSING",
            "Falta MAM_API_KEY: toda la integracion con el proveedor esta deshabilitada."))
    if not settings.MT5_CREDENTIALS_ENCRYPTION_KEY:
        findings.append(Finding(
            "CRITICAL" if prod else "INFO", "MT5_ENCRYPTION_KEY_MISSING",
            "Falta MT5_CREDENTIALS_ENCRYPTION_KEY: no se pueden crear cuentas MT5 "
            "(el provisioning falla cerrado para no guardar contrasenas en claro)."))

    # ── provisioning MT5 (spec §5, paso 1) ──
    # Alcanza con el grupo general O con los dos por capacidad; sin ninguno no se
    # puede crear ninguna cuenta MT5.
    if not (settings.MAM_MT5_PLATFORM_GROUP
            or (settings.MAM_MT5_GROUP_LEADER and settings.MAM_MT5_GROUP_FOLLOWER)):
        findings.append(Finding(
            "CRITICAL" if prod else "WARNING", "MT5_PLATFORM_GROUP_MISSING",
            "Falta el grupo MT5 acordado con el broker (MAM_MT5_PLATFORM_GROUP, o bien "
            "MAM_MT5_GROUP_LEADER + MAM_MT5_GROUP_FOLLOWER): no se pueden crear cuentas."))

    # El servidor MT5 es el tercer dato que el cliente necesita para entrar a la
    # terminal, junto con login y contrasena. Sin el, la cuenta se crea igual y
    # el problema aparece del otro lado: alguien recibe credenciales que no
    # dicen a donde conectarse.
    if not settings.MAM_MT5_SERVER.strip():
        findings.append(Finding(
            "WARNING", "MT5_SERVER_MISSING",
            "Falta MAM_MT5_SERVER: las cuentas se crean, pero las credenciales que se "
            "entregan al cliente no indican a que servidor MT5 conectarse."))

    # ── copy trading (spec §4.1) ──
    if settings.MAM_ACCOUNT_MODE.strip().upper() != "HEDGING":
        findings.append(Finding(
            "CRITICAL" if prod else "WARNING", "ACCOUNT_MODE_NOT_HEDGING",
            f"MAM_ACCOUNT_MODE={settings.MAM_ACCOUNT_MODE!r}. Las cuentas que participan "
            f"en copy trading deben usar HEDGING; con otro modo el motor rechaza las "
            f"allocations."))

    # ── webhook de terminacion (spec §11.6) ──
    if not settings.MAM_WEBHOOK_SIGNING_SECRET:
        findings.append(Finding(
            "CRITICAL" if prod else "INFO", "WEBHOOK_SECRET_MISSING",
            "Falta MAM_WEBHOOK_SIGNING_SECRET: no se puede verificar la firma de los "
            "eventos de terminacion, asi que no se procesan. El secreto se entrega en "
            "claro UNA SOLA VEZ al registrar el webhook."))

    # ── entorno ──
    if prod and settings.DEBUG:
        findings.append(Finding(
            "CRITICAL", "DEBUG_ON_IN_PRODUCTION",
            "DEBUG esta activo en produccion."))

    return findings


def log_findings(findings: list[Finding]) -> None:
    if not findings:
        logger.info("Config check: sin observaciones.")
        return
    for f in findings:
        log = logger.error if f.severity == "CRITICAL" else (
            logger.warning if f.severity == "WARNING" else logger.info)
        log("Config check [%s] %s: %s", f.severity, f.code, f.message)


def summary() -> dict:
    """Resumen apto para exponer en /health (sin filtrar valores de configuracion)."""
    findings = run_checks()
    return {
        "critical": sum(1 for f in findings if f.severity == "CRITICAL"),
        "warnings": sum(1 for f in findings if f.severity == "WARNING"),
        "codes": [f.code for f in findings],
    }
