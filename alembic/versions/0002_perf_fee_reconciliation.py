"""Campos de conciliacion de performance fee.

El detalle por investor del proveedor trae mas de lo que guardabamos: el periodo
que cristalizo cada corrida y los rastros del lado MT5.

Sin el periodo no se puede responder "cuanto cobro este leader en marzo" sin
adivinar por fecha de ejecucion — y la fecha de ejecucion no coincide con el
periodo, porque la corrida se ejecuta DESPUES del corte.

Los ids de MT5 y la clave de cashflow no se usan para deduplicar (para eso esta
provider_payment_id, que ya es UNIQUE) pero son lo unico que permite rastrear un
pago concreto si hay que discutirlo con el broker.

Revision ID: 0002_perf_fee_reconciliation
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_perf_fee_reconciliation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_TABLE = "mam_perf_fee_payments"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("run_status", sa.String(length=30), nullable=True))
    op.add_column(_TABLE, sa.Column("run_period_start", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("run_period_end", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("mt5_transfer_id", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("mt5_op_id", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("cashflow_unique_key", sa.String(length=160), nullable=True))

    op.create_index("ix_mam_perf_fee_payments_status", _TABLE, ["status"], unique=False)
    # Panel de pagos sin asentar: los que no se pudieron atribuir a un cliente.
    op.create_index("ix_mam_perf_fee_payments_unposted", _TABLE,
                    ["ledger_tx_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_mam_perf_fee_payments_unposted", table_name=_TABLE)
    op.drop_index("ix_mam_perf_fee_payments_status", table_name=_TABLE)
    op.drop_column(_TABLE, "cashflow_unique_key")
    op.drop_column(_TABLE, "mt5_op_id")
    op.drop_column(_TABLE, "mt5_transfer_id")
    op.drop_column(_TABLE, "run_period_end")
    op.drop_column(_TABLE, "run_period_start")
    op.drop_column(_TABLE, "run_status")
