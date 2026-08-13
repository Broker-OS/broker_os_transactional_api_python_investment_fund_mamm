"""
Verifica la conciliacion de performance fee contra una base real.

No se puede esperar a que el motor cristalice un fee de verdad — depende de que
la estrategia genere ganancia sobre su High-Water Mark — asi que el detalle por
investor se inyecta con un cliente de prueba que devuelve payloads con la forma
EXACTA del proveedor (tomada de su OpenAPI).

Lo que se prueba es lo que puede salir mal de nuestro lado:

  * que reconciliar dos veces no duplique ni el pago ni el asiento contable;
  * que cada fee quede atribuido al cliente dueño de la cuenta investor;
  * que un pago de una cuenta que no conocemos NO se asiente a nadie;
  * que solo los EXECUTED muevan el libro;
  * que el cuadre contra el credito consolidado detecte una diferencia.

Necesita TEST_DATABASE_URL apuntando a una base dedicada (el nombre debe contener
"test"): recrea el esquema.
"""
import asyncio
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEST_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
if not TEST_URL or "test" not in TEST_URL.rsplit("/", 1)[-1].lower():
    sys.exit("Falta TEST_DATABASE_URL apuntando a una base dedicada (nombre con 'test').")
os.environ["DATABASE_URL"] = TEST_URL

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.models  # noqa: F401,E402
from app.db.database import Base  # noqa: E402
from app.models.mam import ACCOUNT_ACTIVE, MamAccount, MamLeaderProfile, MamPerfFeePayment  # noqa: E402
from app.models.trader import Trader  # noqa: E402
from app.repositories.ledger_repository import CODE_PERF_FEE_PAID, LedgerRepository  # noqa: E402
from app.services import mam_perf_fee_service as mod  # noqa: E402
from app.services.mam_perf_fee_service import MamPerfFeeService  # noqa: E402

fails = []


def check(label, ok, extra=""):
    print(("  [OK] " if ok else "  [FALLO] ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(label)


# ── cliente de prueba: misma forma de payload que el proveedor ──
def pago(pid, investor, monto, *, run_id=7, status="EXECUTED"):
    return {
        "id": pid, "run_id": run_id, "run_status": "COMPLETED",
        "run_period_start": "2026-08-01T00:00:00", "run_period_end": "2026-09-01T00:00:00",
        "investor_id": 1, "investor_mt5_login": investor,
        "amount": monto, "currency": "USD", "status": status,
        "mt5_transfer_id": 900 + pid, "mt5_op_id": 800 + pid,
        "executed_at": "2026-08-16T00:00:08", "cashflow_id": 1,
        "cashflow_unique_key": f"cf-{pid}", "created_at": "2026-08-16T00:00:08",
    }


class ClienteFalso:
    def __init__(self, pagos, creditos=()):
        self._pagos, self._creditos = pagos, list(creditos)

    async def collect_all(self, fetch, **kwargs):
        # Se despacha por NOMBRE, no por identidad: cada acceso a un metodo
        # ligado crea un objeto nuevo, asi que `fetch is self.x` siempre es False.
        nombre = getattr(fetch, "__name__", "")
        return self._creditos if nombre == "list_perf_fee_transactions" else self._pagos

    async def list_investor_payments(self, **kw):
        return {"items": self._pagos, "has_more": False}

    async def list_perf_fee_transactions(self, **kw):
        return {"items": self._creditos, "has_more": False}


async def main():
    engine = create_async_engine(TEST_URL)
    async with engine.begin() as conn:
        # asyncpg no admite dos sentencias en un mismo prepared statement.
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # ── datos base ──
    async with Session() as db:
        await LedgerRepository(db).ensure_global_accounts()
        trader = Trader(external_reference="900000000001", email="cliente@example.com")
        db.add(trader)
        await db.flush()

        leader = MamAccount(mt5_login="500001", can_be_leader=True, status=ACCOUNT_ACTIVE)
        investor = MamAccount(mt5_login="500002", trader_id=trader.id, status=ACCOUNT_ACTIVE)
        db.add_all([leader, investor])
        await db.flush()
        db.add(MamLeaderProfile(account_id=leader.id, leader_id=99, account_login="500001",
                                payment_account_login="500003", strategy_name="Test"))
        await db.commit()
        trader_id = trader.id

    pagos = [
        pago(1, "500002", "95.47"),                      # cliente conocido
        pago(2, "500002", "111.08"),                     # otro pago del mismo
        pago(3, "999999", "50.00"),                      # cuenta que NO tenemos
        pago(4, "500002", "10.00", status="PENDING"),    # todavia no ejecutado
    ]

    print("\n1. Primera conciliacion")
    async with Session() as db:
        svc = MamPerfFeeService(db)
        svc._client = ClienteFalso(pagos)
        r1 = await svc.reconcile(master_login="500001")
    check("trae los 4 pagos", r1["fetched"] == 4, f"fetched={r1['fetched']}")
    check("los 4 son nuevos", r1["new"] == 4)
    check("asienta solo los 2 atribuibles y ejecutados", r1["posted_to_ledger"] == 2,
          f"posted={r1['posted_to_ledger']}")
    check("suma solo los EXECUTED", r1["executed_total"] == Decimal("256.55"),
          f"total={r1['executed_total']}")
    check("reporta el pago sin cliente", r1["pending_attribution"] == ["999999"],
          str(r1["pending_attribution"]))

    print("\n2. Efecto en el libro contable")
    async with Session() as db:
        repo = LedgerRepository(db)
        pf = await repo.account_for_update(code=CODE_PERF_FEE_PAID)
        saldo_pf = await repo.recompute_balance(pf.id)
        hold = await repo.account_for_update(code="TRADER_HOLDINGS", trader_id=trader_id)
        saldo_hold = await repo.recompute_balance(hold.id)
    check("PERF_FEE_PAID acumula los fees cedidos", saldo_pf == Decimal("206.55"),
          f"saldo={saldo_pf}")
    check("el cliente tiene menos capital", saldo_hold == Decimal("-206.55"),
          f"saldo={saldo_hold}")

    print("\n3. Segunda conciliacion sobre los MISMOS pagos (idempotencia)")
    async with Session() as db:
        svc = MamPerfFeeService(db)
        svc._client = ClienteFalso(pagos)
        r2 = await svc.reconcile(master_login="500001")
    check("no hay pagos nuevos", r2["new"] == 0, f"new={r2['new']}")
    check("no asienta de nuevo", r2["posted_to_ledger"] == 0, f"posted={r2['posted_to_ledger']}")
    async with Session() as db:
        repo = LedgerRepository(db)
        pf = await repo.account_for_update(code=CODE_PERF_FEE_PAID)
        saldo_pf2 = await repo.recompute_balance(pf.id)
        n = (await db.execute(select(MamPerfFeePayment))).scalars().all()
    check("el saldo contable no se movio", saldo_pf2 == saldo_pf, f"{saldo_pf} -> {saldo_pf2}")
    check("no se duplicaron filas", len(n) == 4, f"filas={len(n)}")

    print("\n4. El pago huerfano se asienta cuando aparece su cuenta")
    async with Session() as db:
        db.add(MamAccount(mt5_login="999999", trader_id=trader_id, status=ACCOUNT_ACTIVE))
        await db.commit()
    async with Session() as db:
        svc = MamPerfFeeService(db)
        svc._client = ClienteFalso(pagos)
        r3 = await svc.reconcile(master_login="500001")
    check("ahora si lo asienta", r3["posted_to_ledger"] == 1, f"posted={r3['posted_to_ledger']}")
    check("ya no queda sin atribuir", r3["pending_attribution"] == [],
          str(r3["pending_attribution"]))

    print("\n5. Un PENDING que pasa a EXECUTED se asienta recien entonces")
    pagos_av = [p if p["id"] != 4 else pago(4, "500002", "10.00") for p in pagos]
    async with Session() as db:
        svc = MamPerfFeeService(db)
        svc._client = ClienteFalso(pagos_av)
        r4 = await svc.reconcile(master_login="500001")
    check("asienta el que se ejecuto", r4["posted_to_ledger"] == 1, f"posted={r4['posted_to_ledger']}")
    check("sigue sin traer filas nuevas", r4["new"] == 0)

    print("\n6. Cuadre contra el credito consolidado")
    async with Session() as db:
        svc = MamPerfFeeService(db)
        # El motor acredito menos de lo que suma el detalle: tiene que detectarlo.
        svc._client = ClienteFalso(pagos_av, creditos=[{"id": 1, "amount": "200.00"}])
        v = await svc.verify_runs(master_login="500001")
    check("detecta la diferencia", v["matches"] is False, f"dif={v['difference']}")
    check("informa el total acreditado", v["credited_total"] == Decimal("200.00"))
    check("informa el total del detalle", v["detail_total"] == Decimal("266.55"),
          f"detalle={v['detail_total']}")

    async with Session() as db:
        svc = MamPerfFeeService(db)
        svc._client = ClienteFalso(pagos_av, creditos=[{"id": 1, "amount": "266.55"}])
        v2 = await svc.verify_runs(master_login="500001")
    check("cuadra cuando coincide", v2["matches"] is True, f"dif={v2['difference']}")

    await engine.dispose()
    print("\n" + "=" * 60)
    print("FALLOS:", len(fails))
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
