"""
Movimientos de capital contra las cuentas MT5 (spec §5 paso 5, §11.1).

Cada operacion toca DOS sistemas: el motor MAM (que mueve dinero real en MT5) y
nuestro libro contable. El orden en que se tocan es distinto en cada sentido, y
no es un detalle de estilo:

  DEPOSITO   el dinero SALE de la cuenta maestra. Se reserva primero (hold),
             se llama al motor, y recien despues se confirma el asiento. Si el
             motor rechaza, se libera la reserva. Sin el hold, dos depositos
             simultaneos podrian comprometer dos veces el mismo saldo.

  RETIRO     el dinero ENTRA a la cuenta maestra. No hay nada que reservar: se
             llama al motor y se postea lo que efectivamente volvio.

EL FEE NO ES NUESTRO. Un retiro cobra primero el performance fee vencido
(spec §10.1) y la respuesta lo informa aparte, en `performance_fee_charged`. Ese
dinero salio del cliente pero NO llega a la cuenta maestra: se acredita en la
PAYMENT del leader. Por eso genera un asiento propio contra PERF_FEE_PAID, no
contra la maestra. Sumarlo al retiro inflaria el saldo de la tesoreria con plata
que nunca entro.

IDEMPOTENCIA. Los endpoints financieros del motor aceptan `idempotency_key`
(spec §12), asi que reintentar la misma key es seguro. La respuesta del retiro
trae `withdrawal_was_reused`: cuando es true la operacion ya se habia procesado y
NO hay que volver a postear el asiento.
"""
from __future__ import annotations

import logging
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AmountInvalidError,
    InsufficientAccountFundsError,
    InsufficientMasterAccountError,
    LedgerInconsistentError,
    OperationInFlightError,
    PaymentAccountDepositForbiddenError,
    ProviderBusinessRuleError,
    ProviderOperationInProgressError,
    ProviderPayloadError,
    TraderHasNoAccountError,
)
from app.models.mam import MamAccount
from app.models.movement import Movement
from app.repositories.mam_repository import MamRepository
from app.repositories.movement_repository import MovementRepository
from app.repositories.trader_repository import TraderRepository
from app.services.ledger_service import LedgerService
from app.services.mam_account_service import MamAccountService
from app.services.mam_client import get_mam_client

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")


class MamCapitalService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)
        self.movement_repo = MovementRepository(db)
        self.trader_repo = TraderRepository(db)
        self.ledger = LedgerService(db)
        self.accounts = MamAccountService(db)
        self._client = get_mam_client()

    # ══════════════════════════════════════════════════════════════════
    # helpers
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize(amount: Decimal) -> Decimal:
        """A dos decimales, truncando. Redondear hacia arriba moveria mas dinero
        del que pidio el socio."""
        amt = Decimal(str(amount)).quantize(_CENTS, rounding=ROUND_DOWN)
        if amt <= 0:
            raise AmountInvalidError(message="El monto debe ser mayor a 0",
                                     detail=f"amount={amount}")
        return amt

    async def _resolve(self, mt5_login: str, *, caller=None) -> tuple[MamAccount, str]:
        """Cuenta operable + trader dueño.

        El capital se contabiliza POR CLIENTE: sin trader no hay cuenta de
        holdings contra la cual postear, asi que una cuenta sin dueño (estrategia
        propia del broker) no admite depositos ni retiros por esta via.
        """
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        if await self.repo.is_payment_account(acc.mt5_login):
            raise PaymentAccountDepositForbiddenError(
                message="Esa cuenta es la PAYMENT de un leader: solo recibe fees",
                detail=(f"mt5_login={acc.mt5_login}. Para sacar fees usar "
                        f"POST /mam/leaders/{{login}}/payment-account/withdraw."))
        if acc.trader_id is None:
            raise TraderHasNoAccountError(
                message="La cuenta no tiene un cliente asociado y no puede mover capital",
                detail=(f"mt5_login={acc.mt5_login}: el libro contable registra el capital "
                        f"por cliente. Asociar la cuenta a un cliente primero."))
        return acc, acc.trader_id

    async def _guard_in_flight(self, acc: MamAccount) -> None:
        """Una sola operacion en vuelo por cuenta.

        La BD ya lo garantiza con un indice parcial unico; esto solo convierte el
        choque en un error entendible en vez de un IntegrityError.
        """
        pendientes, _ = await self.movement_repo.list(
            trader_id=acc.trader_id, status="PENDING", limit=5)
        for mv in pendientes:
            if mv.mam_account_id == acc.id:
                raise OperationInFlightError(
                    message="Ya hay una operacion en curso sobre esa cuenta",
                    detail=f"movement_id={mv.id} direction={mv.direction}")

    # ══════════════════════════════════════════════════════════════════
    # Deposito: cuenta maestra -> cuenta MT5 del cliente
    # ══════════════════════════════════════════════════════════════════

    async def deposit(self, *, mt5_login: str, amount: Decimal,
                      idempotency_key: Optional[str] = None, caller=None) -> Movement:
        amount = self._normalize(amount)
        acc, trader_id = await self._resolve(mt5_login, caller=caller)

        mv_id = str(uuid.uuid4())
        idem = idempotency_key or f"deposit:{mv_id}"
        existing = await self.movement_repo.get_by_idempotency_key(idem)
        if existing is not None:
            return existing
        await self._guard_in_flight(acc)

        movement = Movement(
            id=mv_id, trader_id=trader_id, direction="DEPOSIT", amount=amount,
            currency=settings.LEDGER_CURRENCY, idempotency_key=idem, status="PENDING",
            mam_account_id=acc.id, mt5_login=acc.mt5_login, crm_reference=idem,
            requested_amount=amount,
            description=f"Deposito en la cuenta MT5 {acc.mt5_login}",
        )
        self.movement_repo.add(movement)

        # HOLD: reserva el saldo de la maestra antes de tocar el motor.
        try:
            ledger_tx_id = await self.ledger.create_deposit_hold(
                trader_id=trader_id, amount=amount, idempotency_key=f"ldg-{idem}")
        except ValueError as exc:
            await self.db.rollback()
            if "INSUFFICIENT_MASTER_ACCOUNT" in str(exc):
                raise InsufficientMasterAccountError(
                    message="La cuenta maestra no tiene saldo disponible suficiente",
                    detail=str(exc)[:300]) from exc
            raise LedgerInconsistentError(
                message="No se pudo procesar el deposito", detail=str(exc)[:400]) from exc
        except RuntimeError as exc:
            await self.db.rollback()
            raise LedgerInconsistentError(
                message="No se pudo procesar el deposito", detail=str(exc)[:400]) from exc

        movement.ledger_tx_id = ledger_tx_id
        await self.db.commit()

        try:
            data = await self._client.deposit(
                account_login=acc.mt5_login, amount=amount, idempotency_key=idem)
        except (ProviderBusinessRuleError, ProviderPayloadError,
                ProviderOperationInProgressError) as exc:
            # El motor rechazo por regla de negocio: el dinero nunca salio, se
            # libera la reserva para que vuelva a estar disponible.
            await self._release(movement, ledger_tx_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            # Resultado incierto. La reserva se MANTIENE a proposito: si el
            # deposito si se ejecuto, liberarla dejaria el saldo comprometido dos
            # veces. Se concilia con el mt5_deal_id.
            movement.status = "AMBIGUOUS"
            movement.error_detail = f"{type(exc).__name__}: {str(exc)[:400]}"
            await self.db.commit()
            logger.error("MAM: deposito %s en %s con resultado incierto; conciliar por "
                         "idempotency_key=%s antes de reintentar", mv_id, acc.mt5_login, idem)
            raise

        try:
            await self.ledger.commit_deposit(ledger_tx_id=ledger_tx_id)
        except (ValueError, RuntimeError) as exc:
            await self.db.rollback()
            raise LedgerInconsistentError(
                message="El deposito se ejecuto pero no se pudo asentar",
                detail=str(exc)[:400]) from exc

        movement.status = "COMPLETED"
        movement.effective_amount = _dec(data.get("amount")) or amount
        movement.mt5_deal_id = _int(data.get("mt5_deal_id"))
        movement.provider_reference = _str(data.get("transaction_id"))
        await self.db.commit()
        await self.db.refresh(movement)
        logger.info("MAM: deposito %s de %s en %s (deal=%s)",
                    mv_id, amount, acc.mt5_login, movement.mt5_deal_id)
        return movement

    async def _release(self, movement: Movement, ledger_tx_id: str, exc: Exception) -> None:
        try:
            await self.ledger.release_deposit(ledger_tx_id=ledger_tx_id)
        except (ValueError, RuntimeError):
            logger.error("MAM: no se pudo liberar la reserva del deposito %s", movement.id)
        movement.status = "FAILED"
        movement.error_detail = (getattr(exc, "detail", None) or str(exc))[:500]
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # Retiro: cuenta MT5 del cliente -> cuenta maestra
    # ══════════════════════════════════════════════════════════════════

    async def withdraw(self, *, mt5_login: str, amount: Decimal,
                       idempotency_key: Optional[str] = None, caller=None) -> Movement:
        """Retira de la cuenta MT5 despues de cobrar el fee vencido.

        El motor rechaza el retiro si el free margin restante no cubre el fee mas
        el monto pedido, asi que no hay retiros parciales: o sale completo o
        falla. Lo que si varia es cuanto capital pierde el cliente en total —
        el monto pedido MAS el fee que se le cobro en el camino.
        """
        amount = self._normalize(amount)
        acc, trader_id = await self._resolve(mt5_login, caller=caller)

        mv_id = str(uuid.uuid4())
        idem = idempotency_key or f"withdrawal:{mv_id}"
        existing = await self.movement_repo.get_by_idempotency_key(idem)
        if existing is not None:
            return existing
        await self._guard_in_flight(acc)

        movement = Movement(
            id=mv_id, trader_id=trader_id, direction="WITHDRAWAL", amount=amount,
            currency=settings.LEDGER_CURRENCY, idempotency_key=idem, status="PENDING",
            mam_account_id=acc.id, mt5_login=acc.mt5_login, crm_reference=idem,
            requested_amount=amount,
            description=f"Retiro de la cuenta MT5 {acc.mt5_login}",
        )
        self.movement_repo.add(movement)
        await self.db.commit()

        try:
            data = await self._client.withdraw(
                account_login=acc.mt5_login, amount=amount, idempotency_key=idem)
        except ProviderOperationInProgressError as exc:
            movement.status = "FAILED"
            movement.error_detail = (exc.detail or "")[:500]
            await self.db.commit()
            raise InsufficientAccountFundsError(
                message="El retiro no entra en el saldo disponible despues del fee",
                detail=exc.detail) from exc
        except (ProviderBusinessRuleError, ProviderPayloadError) as exc:
            movement.status = "FAILED"
            movement.error_detail = (getattr(exc, "detail", None) or str(exc))[:500]
            await self.db.commit()
            raise
        except Exception as exc:  # noqa: BLE001
            movement.status = "AMBIGUOUS"
            movement.error_detail = f"{type(exc).__name__}: {str(exc)[:400]}"
            await self.db.commit()
            logger.error("MAM: retiro %s de %s con resultado incierto; reintentar con la "
                         "MISMA idempotency_key=%s", mv_id, acc.mt5_login, idem)
            raise

        efectivo = _dec(data.get("requested_amount")) or amount
        fee = _dec(data.get("performance_fee_charged")) or Decimal("0")
        reusado = bool(data.get("withdrawal_was_reused"))

        movement.effective_amount = efectivo
        movement.perf_fee_at_request = fee
        movement.balance_before = _dec(data.get("balance_before"))
        movement.balance_after = _dec(data.get("balance_after"))
        movement.mt5_deal_id = _int(data.get("withdrawal_mt5_deal_id"))
        movement.provider_reference = _str(data.get("transaction_id"))
        movement.provider_result = "ALREADY_PROCESSED" if reusado else "COMPLETED"

        if reusado:
            # El motor reconocio la key y no volvio a debitar. Postear el asiento
            # ahora duplicaria el ingreso de una operacion que ya se contabilizo.
            movement.status = "COMPLETED"
            await self.db.commit()
            await self.db.refresh(movement)
            logger.info("MAM: retiro %s reutilizado (idempotency_key=%s), sin asiento nuevo",
                        mv_id, idem)
            return movement

        if efectivo <= 0:
            # El motor respondio OK pero no movio nada.
            movement.status = "REJECTED"
            await self.db.commit()
            await self.db.refresh(movement)
            return movement

        try:
            movement.ledger_tx_id = await self.ledger.post_withdrawal(
                trader_id=trader_id, amount=efectivo, idempotency_key=f"ldg-{idem}")
            if fee > 0:
                # El fee salio del cliente hacia la PAYMENT del leader: NO entra a
                # la cuenta maestra. Asiento aparte contra PERF_FEE_PAID.
                await self.ledger.post_perf_fee(
                    trader_id=trader_id, amount=fee,
                    idempotency_key=f"pf-{idem}",
                    description=f"Fee cobrado al retirar de {acc.mt5_login}")
        except (ValueError, RuntimeError) as exc:
            await self.db.rollback()
            raise LedgerInconsistentError(
                message="El retiro se ejecuto pero no se pudo asentar",
                detail=str(exc)[:400]) from exc

        movement.status = "COMPLETED"
        await self.db.commit()
        await self.db.refresh(movement)
        logger.info("MAM: retiro %s de %s en %s (fee cobrado=%s, deal=%s)",
                    mv_id, efectivo, acc.mt5_login, fee, movement.mt5_deal_id)
        return movement

    # ══════════════════════════════════════════════════════════════════
    # Consulta
    # ══════════════════════════════════════════════════════════════════

    async def balance_transactions(
        self, *, mt5_login: str, transaction_type: Optional[str] = None,
        tx_status: Optional[str] = None, cursor: Optional[int] = None,
        limit: Optional[int] = None, caller=None,
    ) -> dict:
        """Historial contable de la cuenta SEGUN EL MOTOR (spec §11.1).

        Es la contraparte de nuestro `/movements`: aca se ven tambien los
        movimientos que no originamos nosotros — creditos de performance fee,
        ajustes del broker — que en nuestro libro no existen.
        """
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        return await self._client.list_balance_transactions(
            account_login=acc.mt5_login, transaction_type=transaction_type,
            status=tx_status, cursor=cursor, limit=limit)


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


def _str(value: Any) -> Optional[str]:
    return None if value is None else str(value)[:64]
