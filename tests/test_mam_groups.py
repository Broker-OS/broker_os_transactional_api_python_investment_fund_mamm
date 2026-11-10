"""
Grupo MT5 y moneda al crear cuentas, y el candado de moneda para capital y
suscripciones.

No necesita base de datos ni proveedor: el cliente MAM es un doble que registra
si se lo llamo. Lo que importa en casi todos los casos es que un pedido invalido
se rechace ANTES de crear la cuenta en MT5 — crearla no es reversible y el grupo
no se puede mover despues.
"""
import asyncio
import inspect
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pydantic import ValidationError as PydValidationError  # noqa: E402

from app.core import crypto  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.exceptions import (  # noqa: E402
    AccountCurrencyMismatchError,
    AccountCurrencyUnsupportedError,
    PlatformGroupLeaderForbiddenError,
)
from app.services.mam_account_service import (  # noqa: E402
    MamAccountService,
    ensure_ledger_currency,
)

fails = []


def check(label, ok, extra=""):
    print(("  [OK] " if ok else "  [FALLO] ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(label)


LEADER_G = "real\\B\\MASTER"
FOLLOWER_G = "real\\B\\INVESTOR"
OTRO_G = "real\\B\\INVESTOR_VIP"

settings.LEDGER_CURRENCY = "USD"
settings.MAM_MT5_PLATFORM_GROUP = ""
settings.MAM_MT5_GROUP_LEADER = LEADER_G
settings.MAM_MT5_GROUP_FOLLOWER = FOLLOWER_G

crypto.is_configured = lambda: True
crypto.encrypt = lambda v: f"enc:{v}" if v else None


class FakeClient:
    def __init__(self, currency_echo=None):
        self.calls = []
        self.currency_echo = currency_echo

    async def create_account(self, **kw):
        self.calls.append(kw)
        return {"mt5_login": "900001", "id": 1,
                "currency": self.currency_echo or kw["currency"]}


def make_service(client):
    svc = MamAccountService.__new__(MamAccountService)
    svc._client = client

    async def persist(acc, *, what):
        return acc
    svc._persist = persist
    return svc


def create(svc, **kw):
    base = dict(external_reference=None, first_name="A", last_name="B", name="N",
                username="u@x.com")
    base.update(kw)
    return asyncio.run(svc.create_account(**base))


def rejects(exc_type, **kw):
    client = FakeClient()
    try:
        create(make_service(client), **kw)
    except exc_type:
        return not client.calls or "llamo al motor igual"
    except Exception as e:  # noqa: BLE001
        return f"levanto {type(e).__name__}"
    return "no levanto nada"


print("\n1. Estrategia: el grupo lo pone el servicio")
c = FakeClient()
acc = create(make_service(c), can_be_leader=True, can_be_follower=False)
check("sin platform_group nace en el grupo de estrategias",
      c.calls[0]["platform_group"] == LEADER_G and acc.platform_group == LEADER_G)
check("pedir grupo para una estrategia se rechaza sin tocar MT5",
      rejects(PlatformGroupLeaderForbiddenError, can_be_leader=True,
              platform_group=FOLLOWER_G) is True)

print("\n2. Investor: el grupo que se mande")
c = FakeClient()
create(make_service(c), platform_group=f"  {OTRO_G} ")
check("se usa el grupo enviado, recortado", c.calls[0]["platform_group"] == OTRO_G)
c = FakeClient()
create(make_service(c))
check("sin grupo (alta general) va al de investors", c.calls[0]["platform_group"] == FOLLOWER_G)
check("la moneda es la del libro", c.calls[0]["currency"] == "USD")

print("\n3. Moneda")
check("currency distinta al libro se rechaza sin tocar MT5",
      rejects(AccountCurrencyMismatchError, platform_group=OTRO_G, currency="USC") is True)
check("currency del libro (en minusculas) se acepta",
      create(make_service(FakeClient()), currency="usd").currency == "USD")
acc = create(make_service(FakeClient(currency_echo="USC")), platform_group=OTRO_G)
check("si el motor informa otra moneda, la cuenta queda en esa (bloqueada)",
      acc.currency == "USC")

print("\n4. Candado de moneda para capital y suscripciones")
try:
    ensure_ledger_currency(SimpleNamespace(currency="usd", mt5_login="1"), para="x")
    check("una cuenta USD pasa", True)
except Exception as e:  # noqa: BLE001
    check("una cuenta USD pasa", False, type(e).__name__)
for ccy in ("USC", None, ""):
    try:
        ensure_ledger_currency(SimpleNamespace(currency=ccy, mt5_login="1"), para="x")
        check(f"una cuenta en {ccy!r} se bloquea", False)
    except AccountCurrencyUnsupportedError:
        check(f"una cuenta en {ccy!r} se bloquea", True)

from app.services import mam_allocation_service, mam_capital_service  # noqa: E402

check("capital: _resolve llama a ensure_ledger_currency",
      "ensure_ledger_currency" in inspect.getsource(
          mam_capital_service.MamCapitalService._resolve))
check("suscripciones: _ensure_operable llama a ensure_ledger_currency",
      "ensure_ledger_currency" in inspect.getsource(
          mam_allocation_service.MamAllocationService._ensure_operable))

print("\n5. Endpoints separados: estrategia e investor")
from app.api.v1 import mam as routes  # noqa: E402
from app.schemas.mam import (  # noqa: E402
    MamInvestorAccountCreateRequest,
    MamStrategyAccountCreateRequest,
)


def schema_rechaza(model, **kw):
    try:
        model(**titular, **kw)
        return False
    except PydValidationError:
        return True


titular = dict(external_reference="438434273005", first_name="A", last_name="B",
               name="N", username="u@x.com")
for extra in ({"platform_group": FOLLOWER_G}, {"currency": "USC"}, {"can_be_leader": False}):
    check(f"/strategy rechaza {list(extra)[0]}",
          schema_rechaza(MamStrategyAccountCreateRequest, **extra))
check("/investor exige platform_group", schema_rechaza(MamInvestorAccountCreateRequest))
check("/investor rechaza platform_group vacio",
      schema_rechaza(MamInvestorAccountCreateRequest, platform_group=""))
check("/investor rechaza platform_group de solo espacios",
      schema_rechaza(MamInvestorAccountCreateRequest, platform_group="   "))
for extra in ({"can_be_leader": True}, {"currency": "USC"}):
    check(f"/investor rechaza {list(extra)[0]}",
          schema_rechaza(MamInvestorAccountCreateRequest, platform_group=OTRO_G, **extra))

recibido = {}
moneda_creada = {"v": "USD"}


class FakeSvc:
    def __init__(self, db):
        pass

    async def create_account(self, **kw):
        recibido.clear()
        recibido.update(kw)
        return SimpleNamespace(currency=moneda_creada["v"])


async def fake_read(svc, acc):
    return {}

routes.MamAccountService = FakeSvc
routes._read = fake_read

r = asyncio.run(routes.create_strategy_account(
    MamStrategyAccountCreateRequest(**titular), db=None, caller=None))
check("/strategy crea con can_be_leader=true, sin grupo ni moneda",
      recibido.get("can_be_leader") is True and recibido.get("can_be_follower") is False
      and "platform_group" not in recibido and "currency" not in recibido)
r = asyncio.run(routes.create_investor_account(
    MamInvestorAccountCreateRequest(**titular, platform_group=f" {OTRO_G} "),
    db=None, caller=None))
check("/investor crea con can_be_leader=false y pasa el grupo recortado",
      recibido.get("can_be_leader") is False and recibido.get("can_be_follower") is True
      and recibido.get("platform_group") == OTRO_G)
check("respuesta normal si nacio en USD", r.message == "Cuenta creada correctamente")
moneda_creada["v"] = "USC"
r = asyncio.run(routes.create_investor_account(
    MamInvestorAccountCreateRequest(**titular, platform_group=OTRO_G), db=None, caller=None))
check("la respuesta avisa si nacio en otra moneda", "BLOQUEADA" in r.message, r.message)

print("\n" + "=" * 60)
print("FALLOS:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
