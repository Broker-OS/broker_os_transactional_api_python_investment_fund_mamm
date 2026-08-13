"""
Envio de email para el OTP de fondeo de la cuenta maestra.

Transporte preferido: AWS SES (mismas credenciales que BrokerOS). Si SES no esta
configurado se intenta SMTP. Si tampoco, el envio se omite y el codigo se devuelve
en la respuesta del endpoint (modo dev, para probar sin proveedor de correo).

El correo se manda en HTML (con fallback de texto plano). Tanto boto3/SES como
smtplib son bloqueantes → corren en un thread (`asyncio.to_thread`).
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from decimal import Decimal
from email.message import EmailMessage

from app.core.config import settings
from app.core.exceptions import EmailDeliveryError

logger = logging.getLogger(__name__)

_SUBJECT = "Tu código de verificación — fondeo de cuenta maestra"
# Marca que aparece en el encabezado del correo.
_BRAND = "Social Trading · Bridge Markets"


def _ses_enabled() -> bool:
    return bool(
        settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
        and settings.AWS_SES_SENDER_EMAIL
    )


def _smtp_enabled() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def _render_text(code: str, amount: Decimal, currency: str, minutes: int) -> str:
    return (
        f"Solicitaste fondear la cuenta maestra por {amount} {currency}.\n\n"
        f"Tu código de verificación es: {code}\n\n"
        f"Expira en {minutes} minutos. Si no fuiste vos, ignorá este correo."
    )


def _render_html(code: str, amount: Decimal, currency: str, minutes: int) -> str:
    """Email HTML con estilos inline (para máxima compatibilidad con clientes)."""
    return f"""\
<!DOCTYPE html>
<html lang="es">
<body style="margin:0;padding:0;background:#f4f6f8;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#f4f6f8;padding:24px 0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <tr><td align="center">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0"
             style="max-width:480px;width:100%;background:#ffffff;border-radius:12px;
                    overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,0.08);">
        <tr>
          <td style="background:#0f172a;padding:22px 32px;">
            <span style="color:#ffffff;font-size:17px;font-weight:600;letter-spacing:.3px;">
              {_BRAND}
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:32px 32px 8px;">
            <h1 style="margin:0 0 10px;font-size:20px;color:#0f172a;font-weight:600;">
              Código de verificación
            </h1>
            <p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:#475569;">
              Solicitaste fondear la <strong>cuenta maestra</strong> por
              <strong>{amount} {currency}</strong>. Ingresá este código para confirmar la operación:
            </p>
            <div style="text-align:center;margin:0 0 24px;">
              <span style="display:inline-block;background:#f1f5f9;border:1px solid #e2e8f0;
                           border-radius:10px;padding:16px 24px 16px 34px;font-size:34px;
                           font-weight:700;letter-spacing:10px;color:#0f172a;
                           font-family:'Courier New',Consolas,monospace;">{code}</span>
            </div>
            <p style="margin:0 0 6px;font-size:13px;color:#64748b;">
              El código expira en <strong>{minutes} minutos</strong>.
            </p>
            <p style="margin:0;font-size:13px;color:#64748b;">
              Si no solicitaste esta operación, ignorá este correo.
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 32px;border-top:1px solid #eef2f6;">
            <span style="font-size:11px;color:#94a3b8;">
              Mensaje automático — por favor no respondas a este correo.
            </span>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _send_ses(to: str, subject: str, text_body: str, html_body: str) -> None:
    import boto3  # import perezoso: solo se necesita si SES esta activo

    client = boto3.client(
        "ses",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )
    client.send_email(
        Source=settings.AWS_SES_SENDER_EMAIL,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html": {"Data": html_body, "Charset": "UTF-8"},
            },
        },
    )


def _send_smtp(to: str, subject: str, text_body: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
        if settings.SMTP_USE_TLS:
            server.starttls()
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


def _deposit_bodies(*, tx_hash: str, amount: Decimal, symbol: str, network: str,
                    from_address: str, block: int, explorer: str,
                    ledger_tx_id: str | None = None,
                    saldo_maestra: Decimal | None = None) -> tuple[str, str]:
    moneda = settings.LEDGER_CURRENCY
    text_body = (
        f"Se confirmo un deposito on-chain.\n\n"
        f"Monto:       {amount} {symbol}\n"
        f"Red:         {network}\n"
        f"Transaccion: {tx_hash}\n"
        f"Origen:      {from_address}\n"
        f"Bloque:      {block}\n"
    )
    if explorer:
        text_body += f"Explorador:  {explorer}\n"

    if ledger_tx_id:
        text_body += (
            f"\n--- Acreditado en el libro contable ---\n"
            f"Asiento:     {ledger_tx_id}\n"
            f"Movimiento:  MASTER_ACCOUNT_FUNDING, +{amount} {moneda} a la cuenta maestra\n"
        )
        if saldo_maestra is not None:
            text_body += f"Saldo cuenta maestra: {saldo_maestra} {moneda}\n"
    else:
        text_body += ("\nATENCION: NO se acredito en el libro contable "
                      "(CRYPTO_DEPOSIT_AUTO_CREDIT esta apagado). Hay que cargarlo a mano.\n")

    if ledger_tx_id:
        bloque_ledger = f"""
  <h3 style="margin:22px 0 4px;font-size:15px">Acreditado en el libro contable</h3>
  <table cellpadding="6" style="border-collapse:collapse;font-size:14px">
    <tr><td style="color:#5a6472">Movimiento</td>
        <td><strong>+{amount} {moneda}</strong> a la cuenta maestra
            <span style="color:#5a6472">(MASTER_ACCOUNT_FUNDING)</span></td></tr>
    {f'<tr><td style="color:#5a6472">Saldo cuenta maestra</td><td><strong>{saldo_maestra} {moneda}</strong></td></tr>' if saldo_maestra is not None else ''}
    <tr><td style="color:#5a6472">Asiento</td>
        <td style="font-family:ui-monospace,Consolas,monospace">{ledger_tx_id}</td></tr>
  </table>"""
    else:
        bloque_ledger = ("""
  <p style="margin:22px 0 0;padding:10px 14px;border-left:3px solid #d4820a;background:#fdf6ec">
    <strong>No se acredito en el libro contable.</strong> La acreditacion automatica
    esta desactivada: hay que cargar este deposito a mano.</p>""")

    html_body = f"""<!doctype html>
<html><body style="font-family:system-ui,Segoe UI,Arial,sans-serif;color:#1f2430">
  <h2 style="margin:0 0 4px">Deposito on-chain confirmado</h2>
  <p style="margin:0 0 16px;color:#5a6472">Verificado contra la cadena.</p>
  <table cellpadding="6" style="border-collapse:collapse;font-size:14px">
    <tr><td style="color:#5a6472">Monto</td>
        <td><strong>{amount} {symbol}</strong></td></tr>
    <tr><td style="color:#5a6472">Red</td><td>{network}</td></tr>
    <tr><td style="color:#5a6472">Transaccion</td>
        <td style="font-family:ui-monospace,Consolas,monospace">{tx_hash}</td></tr>
    <tr><td style="color:#5a6472">Origen</td>
        <td style="font-family:ui-monospace,Consolas,monospace">{from_address}</td></tr>
    <tr><td style="color:#5a6472">Bloque</td><td>{block}</td></tr>
  </table>{bloque_ledger}
  {f'<p style="margin-top:16px"><a href="{explorer}">Ver en el explorador</a></p>' if explorer else ''}
</body></html>"""
    return text_body, html_body


async def send_crypto_deposit_email(
    *, to: str, tx_hash: str, amount: Decimal, symbol: str, network: str,
    from_address: str, block: int, explorer: str = "",
    ledger_tx_id: str | None = None, saldo_maestra: Decimal | None = None,
) -> bool:
    """Avisa a un admin que se confirmo un deposito on-chain.

    Si `ledger_tx_id` viene, el mail informa ademas que la cuenta maestra quedo
    acreditada y con que saldo. Si no viene, avisa explicitamente que hay que
    cargarlo a mano: un deposito que entro y no esta en los libros no puede
    pasar desapercibido.

    Devuelve True si se envio; False si no hay transporte configurado (modo dev).
    """
    subject = f"Deposito confirmado: {amount} {symbol} ({network})"
    text_body, html_body = _deposit_bodies(
        tx_hash=tx_hash, amount=amount, symbol=symbol, network=network,
        from_address=from_address, block=block, explorer=explorer,
        ledger_tx_id=ledger_tx_id, saldo_maestra=saldo_maestra)

    if _ses_enabled():
        transport, sender = "SES", _send_ses
    elif _smtp_enabled():
        transport, sender = "SMTP", _send_smtp
    else:
        logger.warning("Sin transporte de email configurado: aviso de deposito NO enviado.")
        return False

    try:
        await asyncio.to_thread(sender, to, subject, text_body, html_body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallo el aviso de deposito (%s): %s", transport, type(exc).__name__)
        raise EmailDeliveryError(
            message="No se pudo enviar el aviso por email",
            detail=f"{transport}: {type(exc).__name__}") from exc
    return True


async def send_otp_email(*, to: str, code: str, amount: Decimal, currency: str) -> bool:
    """Envia el OTP por email (HTML + texto).

    Devuelve True si se envio (SES o SMTP); False si no hay transporte configurado
    (modo dev). Lanza EmailDeliveryError si el transporte esta configurado pero falla.
    """
    minutes = settings.OTP_EXPIRY_MINUTES
    text_body = _render_text(code, amount, currency, minutes)
    html_body = _render_html(code, amount, currency, minutes)

    if _ses_enabled():
        transport, sender = "SES", _send_ses
    elif _smtp_enabled():
        transport, sender = "SMTP", _send_smtp
    else:
        logger.warning("Sin transporte de email configurado: OTP NO enviado (modo dev).")
        return False

    try:
        await asyncio.to_thread(sender, to, _SUBJECT, text_body, html_body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallo el envio de email (%s): %s", transport, type(exc).__name__)
        raise EmailDeliveryError(
            message="No se pudo enviar el email con el OTP", detail=f"{transport}:{type(exc).__name__}"
        )
    logger.info("OTP de fondeo enviado por %s a %s", transport, to)
    return True
