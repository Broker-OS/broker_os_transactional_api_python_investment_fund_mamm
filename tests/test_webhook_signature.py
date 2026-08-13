"""
Verifica la firma del webhook. Corre sin base ni red.

Es el unico endpoint publico del servicio: cualquiera puede llamarlo. Lo unico
que separa un evento del proveedor de uno inventado es esta verificacion, asi
que conviene probarla contra los ataques que existen de verdad — cuerpo alterado,
reproduccion de una captura vieja, y secreto equivocado.
"""
import hashlib
import hmac
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import settings  # noqa: E402
from app.core.exceptions import (  # noqa: E402
    WebhookSecretMissingError,
    WebhookSignatureInvalidError,
    WebhookTimestampSkewError,
)
from app.services.mam_webhook_service import MamWebhookService  # noqa: E402

SECRETO = "KEtHnuOWtgAN4dTHLGRP5rILSqTD09aSRdfAthOBdto"
fails = []


def check(label, ok, extra=""):
    print(("  [OK] " if ok else "  [FALLO] ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(label)


def raises(exc_type, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc_type:
        return True
    except Exception as e:  # noqa: BLE001
        return f"levanto {type(e).__name__}"
    return "no levanto nada"


def firmar(raw: bytes, ts: int, secreto: str = SECRETO) -> str:
    """Reproduce exactamente lo que documenta la spec §11.6."""
    return "sha256=" + hmac.new(
        secreto.encode(), str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()


# Payload de la spec, tal cual.
PAYLOAD = {
    "event_id": "8b9f1de6-2d11-5465-acc6-6ec7f6a32a28",
    "type": "mam.allocation.terminated",
    "version": 1,
    "occurred_at": "2026-07-28T00:19:05.020369Z",
    "data": {
        "allocation_id": 64, "reason": "USER_UNSUBSCRIBE", "triggered_by": "USER",
        "status": "CANCELLED", "leader_login": "146493", "follower_login": "146537",
        "unsubscribe_policy": "CLOSE_ON_UNSUBSCRIBE", "allocation_mode": "EQUITY",
        "stop_loss_value": None, "trigger_equity": None,
        "closed_positions": 0, "failed_positions": 0,
        "performance_fee_due": "0.00", "performance_fee_charged": "0E-8",
        "terminated_at": "2026-07-28T00:19:05.020369Z",
    },
}
RAW = json.dumps(PAYLOAD).encode()
verify = MamWebhookService.verify_signature

print("\n1. Sin secreto configurado, no se acepta nada")
settings.MAM_WEBHOOK_SIGNING_SECRET = ""
ahora = int(time.time())
check("falla cerrado",
      raises(WebhookSecretMissingError, verify,
             raw_body=RAW, timestamp=str(ahora), signature=firmar(RAW, ahora)))

settings.MAM_WEBHOOK_SIGNING_SECRET = SECRETO

print("\n2. Una firma legitima pasa")
ahora = int(time.time())
try:
    verify(raw_body=RAW, timestamp=str(ahora), signature=firmar(RAW, ahora))
    check("acepta el evento del proveedor", True)
except Exception as e:  # noqa: BLE001
    check("acepta el evento del proveedor", False, type(e).__name__)

print("\n3. Cuerpo alterado despues de firmar")
alterado = json.dumps({**PAYLOAD, "data": {**PAYLOAD["data"], "allocation_id": 999}}).encode()
check("rechaza el cuerpo cambiado",
      raises(WebhookSignatureInvalidError, verify,
             raw_body=alterado, timestamp=str(ahora), signature=firmar(RAW, ahora)))

print("\n4. Re-serializar el JSON invalida la firma")
# El mismo contenido, otro espaciado: por esto se firma sobre los bytes crudos
# y no sobre el objeto deserializado.
reserializado = json.dumps(PAYLOAD, indent=2).encode()
check("un JSON equivalente pero reformateado no valida",
      raises(WebhookSignatureInvalidError, verify,
             raw_body=reserializado, timestamp=str(ahora), signature=firmar(RAW, ahora)))

print("\n5. Reproduccion de una captura vieja")
viejo = int(time.time()) - 600  # 10 minutos
check("rechaza un evento de hace 10 minutos",
      raises(WebhookTimestampSkewError, verify,
             raw_body=RAW, timestamp=str(viejo), signature=firmar(RAW, viejo)))
futuro = int(time.time()) + 600
check("rechaza un timestamp del futuro",
      raises(WebhookTimestampSkewError, verify,
             raw_body=RAW, timestamp=str(futuro), signature=firmar(RAW, futuro)))
borde = int(time.time()) - (settings.MAM_WEBHOOK_MAX_SKEW_SECONDS - 20)
try:
    verify(raw_body=RAW, timestamp=str(borde), signature=firmar(RAW, borde))
    check("acepta dentro de la ventana", True)
except Exception as e:  # noqa: BLE001
    check("acepta dentro de la ventana", False, type(e).__name__)

print("\n6. Secreto equivocado")
ahora = int(time.time())
check("rechaza una firma hecha con otro secreto",
      raises(WebhookSignatureInvalidError, verify,
             raw_body=RAW, timestamp=str(ahora), signature=firmar(RAW, ahora, "otro-secreto")))

print("\n7. Encabezados faltantes o basura")
check("sin firma", raises(WebhookSignatureInvalidError, verify,
                          raw_body=RAW, timestamp=str(ahora), signature=None))
check("sin timestamp", raises(WebhookSignatureInvalidError, verify,
                              raw_body=RAW, timestamp=None, signature=firmar(RAW, ahora)))
check("timestamp no numerico", raises(WebhookTimestampSkewError, verify,
                                      raw_body=RAW, timestamp="ayer",
                                      signature=firmar(RAW, ahora)))
check("firma sin el prefijo sha256=",
      raises(WebhookSignatureInvalidError, verify, raw_body=RAW, timestamp=str(ahora),
             signature=firmar(RAW, ahora).removeprefix("sha256=")))

print("\n" + "=" * 60)
print("FALLOS:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
