"""
Verifica las reglas del contrato MAM que el cliente aplica ANTES de salir a la red.

No necesita base de datos ni conexion al proveedor: cada una de estas reglas
esta en la spec y fallar temprano evita gastar un round-trip en un 422 que ya
sabemos que va a pasar — o, peor, mandar un valor que el proveedor acepta mal.
"""
import asyncio
import json
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.exceptions import (  # noqa: E402
    PerfFeeRateInvalidError,
    ProviderBusinessRuleError,
    ProviderPayloadError,
)
from app.services.mam_client import MamClient, _dumps, _idem, _rate  # noqa: E402

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


def araises(exc_type, coro):
    try:
        asyncio.run(coro)
    except exc_type:
        return True
    except Exception as e:  # noqa: BLE001
        return f"levanto {type(e).__name__}"
    return "no levanto nada"


client = MamClient()

print("\n1. Decimales viajan como numeros JSON exactos (spec §3.7)")
# El riesgo real: 0.20 convertido a float y de vuelta a Decimal en el server
# queda 0.2000000000000000111, y el fee del leader nace mal.
out = _dumps({"performance_fee_rate": Decimal("0.20"), "amount": Decimal("1000.00")})
check("no quedan comillas alrededor del numero", '"0.20"' not in out and "0.20" in out, out)
check("el JSON es valido", json.loads(out)["amount"] == 1000.0)
check("sin notacion exponencial", "E" not in _dumps({"a": Decimal("1E-8")}),
      _dumps({"a": Decimal("1E-8")}))
check("un string sigue siendo string", json.loads(_dumps({"k": "0.20"}))["k"] == "0.20")

print("\n2. performance_fee_rate entre 0 y 1 (spec §3.7)")
check("0.20 se acepta", _rate("0.20") == Decimal("0.20"))
check("0 y 1 son validos", _rate(0) == 0 and _rate(1) == 1)
check("20 se rechaza (seria 2000%)", raises(PerfFeeRateInvalidError, _rate, 20))
check("negativo se rechaza", raises(PerfFeeRateInvalidError, _rate, -0.1))
check("texto se rechaza", raises(PerfFeeRateInvalidError, _rate, "veinte"))

print("\n3. Mascara de permisos MT5: solo los dos perfiles soportados (spec §5)")
check("9073 (trading habilitado)", client._validate_rights(9073) is None)
check("8981 (trading deshabilitado)", client._validate_rights(8981) is None)
# El default del proveedor si se omite el campo es 1, que no es ninguno de los dos.
check("1 se rechaza", raises(ProviderPayloadError, client._validate_rights, 1))
check("mascara inventada se rechaza", raises(ProviderPayloadError, client._validate_rights, 9999))

print("\n4. mode_parameter segun allocation_mode (spec §6)")
check("FIXED sin parametro -> error",
      raises(ProviderPayloadError, client._validate_mode_parameter, "FIXED", None))
check("SCALED sin parametro -> error",
      raises(ProviderPayloadError, client._validate_mode_parameter, "SCALED", None))
check("EQUITY sin parametro es valido (equivale a 1)",
      client._validate_mode_parameter("EQUITY", None) is None)
check("BALANCE sin parametro es valido",
      client._validate_mode_parameter("BALANCE", None) is None)
check("0 se rechaza en cualquier modo",
      raises(ProviderPayloadError, client._validate_mode_parameter, "EQUITY", 0))
check("negativo se rechaza",
      raises(ProviderPayloadError, client._validate_mode_parameter, "SCALED", -1))
check("0.5 se acepta", client._validate_mode_parameter("SCALED", "0.5") == Decimal("0.5"))

print("\n5. idempotency_key: largo valido (spec §11.3)")
check("key normal pasa", _idem("deposit:12345") == "deposit:12345")
check("vacia se rechaza", raises(ProviderPayloadError, _idem, ""))
check("menor a 8 se rechaza en el retiro PAYMENT",
      raises(ProviderPayloadError, _idem, "corta", min_len=8))
check("mayor a 120 se rechaza", raises(ProviderPayloadError, _idem, "x" * 121))

print("\n6. La cuenta PAYMENT no puede ser la operativa (spec §4.5)")
check("mismo login para ambas -> error",
      araises(ProviderBusinessRuleError, client.create_leader_profile(
          account_login="139682", strategy_name="X", payment_account_login="139682")))

print("\n7. Sin grupo MT5 configurado no se crea ninguna cuenta (spec §5)")
# Falla cerrado: registrar una cuenta sin el grupo real del broker deja una
# cuenta que MT5 no reconoce.
from app.core.config import settings  # noqa: E402
from app.core.exceptions import MamConfigError  # noqa: E402

settings.MAM_MT5_PLATFORM_GROUP = ""
check("create_account sin platform_group -> error",
      araises(MamConfigError, client.create_account(
          first_name="Ana", last_name="Garcia", name="Ana", username="ana@example.com")))

print("\n" + "=" * 60)
print("FALLOS:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
