"""
Conciliacion de performance fee (spec §10, §11.3).

EL PROBLEMA QUE RESUELVE. El credito que llega a la cuenta PAYMENT del leader
viene CONSOLIDADO: una sola fila por corrida, sin decir cuanto aporto cada
cliente. Para repartir comisiones a sponsors o a una red de IBs eso no sirve —
hace falta el detalle por investor, que vive en otro endpoint.

    /perf-fee/transactions                        -> lo que COBRO el leader (agregado)
    /perf-fee/master/{login}/investor-payments    -> quien lo PAGO (individual)

La conciliacion cruza los dos: la suma de los pagos EXECUTED de un mismo
`run_id` tiene que dar el credito consolidado de esa corrida. Si no cuadra, hay
algo que el proveedor no nos esta contando y conviene saberlo antes de repartir.

IDEMPOTENCIA. Se puede correr N veces sobre el mismo periodo sin duplicar nada:
los pagos se deduplican por el id del proveedor (UNIQUE en la BD) y el asiento
contable se postea con una key derivada de ese id.

ATRIBUCION. Cada pago se asocia al cliente dueño de la cuenta investor. Si esa
cuenta no esta en nuestra base, el pago se REGISTRA igual pero queda SIN
ASENTAR: preferimos un pago visible sin asiento que un asiento atribuido al
cliente equivocado. Quedan listados en `pending_attribution`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import LeaderProfileNotFoundError
from app.models.mam import MamAccount, MamPerfFeePayment
from app.models.trader import Trader
from app.repositories.mam_repository import MamRepository
from app.services.ledger_service import LedgerService
from app.services.mam_client import get_mam_client

logger = logging.getLogger(__name__)

# Solo estos movieron dinero de verdad; el resto no se asienta.
_EXECUTED = "EXECUTED"


class MamPerfFeeService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)
        self.ledger = LedgerService(db)
        self._client = get_mam_client()

    # ══════════════════════════════════════════════════════════════════
    # Conciliacion
    # ══════════════════════════════════════════════════════════════════

    async def reconcile(
        self, *, master_login: str, from_at: Optional[str] = None,
        to_at: Optional[str] = None, run_id: Optional[int] = None,
        post_ledger: bool = True, caller=None,
    ) -> dict:
        """Trae los pagos por investor y los asienta.

        `from_at` es inclusivo y `to_at` exclusivo. Sin fechas ni run_id trae
        todo lo que el proveedor tenga paginado, con el tope de paginas del
        cliente.
        """
        profile = await self.repo.get_profile_by_login(master_login)
        if profile is None:
            raise LeaderProfileNotFoundError(
                message="Esa cuenta no es una estrategia registrada acá",
                detail=(f"account_login={master_login}. Importarla con "
                        f"POST /mam/leaders/import antes de conciliar."))

        pagos = await self._client.collect_all(
            self._client.list_investor_payments, master_login=profile.account_login,
            run_id=run_id, from_at=from_at, to_at=to_at)

        nuevos = 0
        ya_estaban = 0
        asentados = 0
        sin_atribuir: list[str] = []
        total = Decimal("0")

        for item in pagos:
            pid = _int(item.get("id"))
            if pid is None:
                continue

            fila = (await self.db.execute(
                select(MamPerfFeePayment).where(
                    MamPerfFeePayment.provider_payment_id == pid))).scalar_one_or_none()

            if fila is None:
                fila = MamPerfFeePayment(
                    provider_payment_id=pid,
                    run_id=_int(item.get("run_id")),
                    run_status=_str(item.get("run_status"), 30),
                    run_period_start=_dt(item.get("run_period_start")),
                    run_period_end=_dt(item.get("run_period_end")),
                    master_login=profile.account_login,
                    payment_account_login=profile.payment_account_login,
                    investor_mt5_login=_str(item.get("investor_mt5_login"), 40),
                    amount=_dec(item.get("amount")) or Decimal("0"),
                    currency=_str(item.get("currency"), 10) or "USD",
                    status=_str(item.get("status"), 30),
                    executed_at=_dt(item.get("executed_at")),
                    mt5_transfer_id=_int(item.get("mt5_transfer_id")),
                    mt5_op_id=_int(item.get("mt5_op_id")),
                    cashflow_unique_key=_str(item.get("cashflow_unique_key"), 160),
                )
                self.repo.add(fila)
                nuevos += 1
            else:
                # El estado de una corrida puede avanzar entre pasadas.
                fila.status = _str(item.get("status"), 30) or fila.status
                fila.run_status = _str(item.get("run_status"), 30) or fila.run_status
                fila.executed_at = _dt(item.get("executed_at")) or fila.executed_at
                ya_estaban += 1

            if fila.status == _EXECUTED:
                total += Decimal(str(fila.amount or 0))

            # Atribucion: del login investor al cliente dueño.
            if fila.trader_id is None and fila.investor_mt5_login:
                cuenta = await self.repo.get_account_by_login(fila.investor_mt5_login)
                if cuenta is not None:
                    fila.trader_id = cuenta.trader_id

            if fila.status != _EXECUTED or fila.ledger_tx_id is not None:
                continue
            if fila.trader_id is None:
                # Sin dueño no hay contra que postear. Se deja registrado para
                # que aparezca en el panel en vez de perderse.
                sin_atribuir.append(fila.investor_mt5_login or f"pago {pid}")
                continue

            if post_ledger:
                # El fee salio del cliente hacia la PAYMENT del leader: no vuelve
                # a la cuenta maestra. La key deriva del id del proveedor, asi
                # que reconciliar de nuevo no duplica el asiento.
                fila.ledger_tx_id = await self.ledger.post_perf_fee(
                    trader_id=fila.trader_id,
                    amount=Decimal(str(fila.amount)),
                    idempotency_key=f"mam-pf-{pid}",
                    description=(f"Performance fee de {fila.investor_mt5_login} "
                                 f"hacia {profile.account_login}"),
                )
                asentados += 1

        await self.db.commit()

        if sin_atribuir:
            logger.warning(
                "MAM: %s pago(s) de performance fee sin cliente atribuido para %s: %s. "
                "Importar esas cuentas y volver a conciliar.",
                len(sin_atribuir), profile.account_login, ", ".join(sin_atribuir[:10]))

        return {
            "master_login": profile.account_login,
            "payment_account_login": profile.payment_account_login,
            "fetched": len(pagos),
            "new": nuevos,
            "already_known": ya_estaban,
            "posted_to_ledger": asentados,
            "executed_total": total,
            "pending_attribution": sorted(set(sin_atribuir)),
        }

    async def reconcile_all(
        self, *, from_at: Optional[str] = None, to_at: Optional[str] = None,
        caller=None,
    ) -> dict:
        """Concilia todas las estrategias registradas. Pensado para el cron.

        Una estrategia que falla no frena a las demas: se anota y se sigue. Si
        una sola caida del motor abortara la corrida entera, un leader con un
        problema puntual dejaria sin conciliar a todos los demas.
        """
        perfiles, _ = await self.repo.list_profiles(limit=500)
        resumen = {"leaders": len(perfiles), "reconciled": 0, "failed": 0,
                   "fetched": 0, "posted_to_ledger": 0, "errors": [],
                   "pending_attribution": []}

        for perfil in perfiles:
            try:
                r = await self.reconcile(master_login=perfil.account_login,
                                         from_at=from_at, to_at=to_at, caller=caller)
            except Exception as exc:  # noqa: BLE001
                resumen["failed"] += 1
                resumen["errors"].append(f"{perfil.account_login}: {type(exc).__name__}")
                logger.warning("MAM: fallo la conciliacion de %s (%s)",
                               perfil.account_login, type(exc).__name__)
                continue
            resumen["reconciled"] += 1
            resumen["fetched"] += r["fetched"]
            resumen["posted_to_ledger"] += r["posted_to_ledger"]
            resumen["pending_attribution"] += r["pending_attribution"]

        resumen["pending_attribution"] = sorted(set(resumen["pending_attribution"]))
        return resumen

    # ══════════════════════════════════════════════════════════════════
    # Cuadre contra el credito consolidado
    # ══════════════════════════════════════════════════════════════════

    async def verify_runs(
        self, *, master_login: str, from_at: Optional[str] = None,
        to_at: Optional[str] = None, caller=None,
    ) -> dict:
        """Cruza el detalle por investor contra el credito agregado (spec §11.3).

        "La suma de amount de los items EXECUTED del mismo run_id debe coincidir
        con el credito consolidado correspondiente". Cuando no coincide, lo mas
        probable es que falte traer pagos — pero tambien podria ser que el motor
        haya acreditado algo que no nos esta detallando, y eso hay que verlo
        antes de repartir comisiones sobre un total que no cierra.
        """
        profile = await self.repo.get_profile_by_login(master_login)
        if profile is None:
            raise LeaderProfileNotFoundError(
                message="Esa cuenta no es una estrategia registrada acá",
                detail=f"account_login={master_login}")

        creditos = await self._client.collect_all(
            self._client.list_perf_fee_transactions,
            master_login=profile.account_login, from_at=from_at, to_at=to_at)

        # Nuestro detalle, agrupado por corrida.
        filas = (await self.db.execute(
            select(MamPerfFeePayment.run_id,
                   func.sum(MamPerfFeePayment.amount),
                   func.count())
            .where(MamPerfFeePayment.master_login == profile.account_login,
                   MamPerfFeePayment.status == _EXECUTED)
            .group_by(MamPerfFeePayment.run_id))).all()
        por_run = {r[0]: {"detail_total": Decimal(str(r[1] or 0)), "payments": r[2]}
                   for r in filas}

        credito_total = sum((_dec(c.get("amount")) or Decimal("0")) for c in creditos)
        detalle_total = sum(v["detail_total"] for v in por_run.values())

        return {
            "master_login": profile.account_login,
            "payment_account_login": profile.payment_account_login,
            "credits_found": len(creditos),
            "credited_total": credito_total,
            "detail_total": detalle_total,
            "difference": credito_total - detalle_total,
            "matches": credito_total == detalle_total,
            "runs": [{"run_id": k, "detail_total": v["detail_total"], "payments": v["payments"]}
                     for k, v in sorted(por_run.items(), key=lambda x: (x[0] is None, x[0]))],
        }

    # ══════════════════════════════════════════════════════════════════
    # Consulta
    # ══════════════════════════════════════════════════════════════════

    async def list_payments(
        self, *, master_login: Optional[str] = None, investor_login: Optional[str] = None,
        run_id: Optional[int] = None, only_unposted: bool = False,
        owner_api_user_id: Optional[str] = None, page: int = 1, limit: int = 50,
    ) -> tuple[list[MamPerfFeePayment], int, Decimal]:
        """Pagos ya conciliados, desde NUESTRA base. Devuelve tambien el total."""
        filtros = []
        if master_login:
            filtros.append(MamPerfFeePayment.master_login == str(master_login))
        if investor_login:
            filtros.append(MamPerfFeePayment.investor_mt5_login == str(investor_login))
        if run_id is not None:
            filtros.append(MamPerfFeePayment.run_id == run_id)
        if only_unposted:
            # Lo que hay que resolver a mano: cobrado pero sin asiento.
            filtros.append(MamPerfFeePayment.ledger_tx_id.is_(None))
            filtros.append(MamPerfFeePayment.status == _EXECUTED)

        # Scoping por dueño. Un pago SIN atribuir no tiene trader_id, asi que no
        # es de ningun socio y no le aparece a ninguno: podria ser de cualquiera,
        # y mostrarselo a uno seria adivinar. Quedan a la vista del ADMIN, que no
        # lleva filtro, y en /mam/ops/pending.
        if owner_api_user_id:
            filtros.append(MamPerfFeePayment.trader_id.in_(
                select(Trader.id).where(Trader.owner_api_user_id == owner_api_user_id)))

        total = (await self.db.execute(
            select(func.count()).select_from(MamPerfFeePayment).where(*filtros))).scalar_one()
        suma = (await self.db.execute(
            select(func.coalesce(func.sum(MamPerfFeePayment.amount), 0))
            .where(*filtros, MamPerfFeePayment.status == _EXECUTED))).scalar_one()
        filas = (await self.db.execute(
            select(MamPerfFeePayment).where(*filtros)
            .order_by(MamPerfFeePayment.executed_at.desc().nullslast(),
                      MamPerfFeePayment.provider_payment_id.desc())
            .offset((page - 1) * limit).limit(limit))).scalars().all()
        return list(filas), total, Decimal(str(suma or 0))


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any, limit: int) -> Optional[str]:
    return None if value is None else str(value)[:limit]


def _dt(value: Any) -> Optional[datetime]:
    """ISO 8601. Sin zona se asume UTC (spec §3.7)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
