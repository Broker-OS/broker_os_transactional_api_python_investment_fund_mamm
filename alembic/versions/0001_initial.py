"""Esquema inicial del servicio: contabilidad, clientes y motor MAM.

Base de datos NUEVA y consolidada: no arrastra el historial de migraciones del
bridge PAMM. Todo el esquema se crea de una sola vez y refleja exactamente los
modelos de `app/models`.

Las reglas del contrato MAM viajan como CHECK constraints e indices parciales,
no solo como validaciones de servicio: asi no hay forma de violarlas ni por un
bug ni por una carga manual. Las mas importantes:

  * una cuenta no puede seguirse a si misma        -> ck_mam_allocations_not_self
  * una sola allocation VIVA por pareja            -> uq_mam_allocations_live_pair
  * FIXED y SCALED exigen mode_parameter           -> ck_mam_allocations_mode_param_required
  * mode_parameter siempre > 0                     -> ck_mam_allocations_mode_param_positive
  * el performance fee es decimal entre 0 y 1      -> ck_mam_leader_rate / ck_mam_allocations_rate
  * la PAYMENT no es la cuenta operativa           -> ck_mam_leader_payment_distinct
  * una PAYMENT no se comparte entre leaders       -> uq_mam_leader_payment_login
  * solo las dos mascaras MT5 soportadas           -> ck_mam_accounts_rights
  * un webhook no se procesa dos veces             -> ix_mam_webhook_events_event_id

Revision ID: 0001_initial
"""
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'api_users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False, server_default=sa.text("'USER'")),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column('receives_notifications', sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('ADMIN','USER')", name='ck_api_users_role'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_api_users_email', 'api_users', ['email'], unique=True)

    op.create_table(
        'funding_otps',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('amount', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column('code_hash', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('movement_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('PENDING','VERIFIED','CANCELLED')", name='ck_funding_otps_status'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_funding_otps_status', 'funding_otps', ['status'], unique=False)

    op.create_table(
        'mam_webhook_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=60), nullable=False),
        sa.Column('version', sa.Integer(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('allocation_id', sa.BigInteger(), nullable=True),
        sa.Column('reason', sa.String(length=40), nullable=True),
        sa.Column('triggered_by', sa.String(length=20), nullable=True),
        sa.Column('allocation_status', sa.String(length=20), nullable=True),
        sa.Column('leader_login', sa.String(length=40), nullable=True),
        sa.Column('follower_login', sa.String(length=40), nullable=True),
        sa.Column('performance_fee_charged', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('signature_verified', sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('process_error', sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mam_webhook_events_allocation_id', 'mam_webhook_events', ['allocation_id'], unique=False)
    op.create_index('ix_mam_webhook_events_event_id', 'mam_webhook_events', ['event_id'], unique=True)
    op.create_index('ix_mam_webhook_events_event_type', 'mam_webhook_events', ['event_type'], unique=False)
    op.create_index('ix_mam_webhook_events_follower_login', 'mam_webhook_events', ['follower_login'], unique=False)
    op.create_index('ix_mam_webhook_events_leader_login', 'mam_webhook_events', ['leader_login'], unique=False)
    op.create_index('ix_mam_webhook_events_unprocessed', 'mam_webhook_events', ['processed_at', 'received_at'], unique=False)

    op.create_table(
        'api_keys',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('api_user_id', sa.String(length=36), nullable=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('key_prefix', sa.String(length=16), nullable=False),
        sa.Column('key_hash', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(['api_user_id'], ['api_users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_api_keys_api_user_id', 'api_keys', ['api_user_id'], unique=False)
    op.create_index('ix_api_keys_key_prefix', 'api_keys', ['key_prefix'], unique=False)

    op.create_table(
        'mam_deletion_operations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('operation_id', sa.String(length=64), nullable=True),
        sa.Column('target_kind', sa.String(length=20), nullable=False),
        sa.Column('target_login', sa.String(length=40), nullable=False),
        sa.Column('scope', sa.String(length=30), nullable=True),
        sa.Column('investor_logins', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('transmitted_positions_policy', sa.String(length=30), nullable=False, server_default=sa.text("'CLOSE_TRANSMITTED'")),
        sa.Column('idempotency_key', sa.String(length=120), nullable=False),
        sa.Column('requested_by', sa.String(length=120), nullable=True),
        sa.Column('requested_by_api_user_id', sa.String(length=36), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('impact_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("transmitted_positions_policy IN ('CLOSE_TRANSMITTED','KEEP_OPEN')", name='ck_mam_deletions_policy'),
        sa.CheckConstraint("scope IS NULL OR scope IN ('MASTER_ACCOUNT_ONLY','MASTER_AND_INVESTORS')", name='ck_mam_deletions_scope'),
        sa.CheckConstraint("status IN ('PENDING','WAITING_CLOSE','PARTIAL','PURGING','COMPLETED','FAILED')", name='ck_mam_deletions_status'),
        sa.CheckConstraint("target_kind IN ('MASTER','INVESTOR')", name='ck_mam_deletions_target_kind'),
        sa.ForeignKeyConstraint(['requested_by_api_user_id'], ['api_users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mam_deletion_operations_idempotency_key', 'mam_deletion_operations', ['idempotency_key'], unique=True)
    op.create_index('ix_mam_deletion_operations_operation_id', 'mam_deletion_operations', ['operation_id'], unique=False)
    op.create_index('ix_mam_deletion_operations_requested_by_api_user_id', 'mam_deletion_operations', ['requested_by_api_user_id'], unique=False)
    op.create_index('ix_mam_deletion_operations_status', 'mam_deletion_operations', ['status'], unique=False)
    op.create_index('ix_mam_deletion_operations_target_login', 'mam_deletion_operations', ['target_login'], unique=False)
    op.create_index('ix_mam_deletions_status_created', 'mam_deletion_operations', ['status', 'created_at'], unique=False)

    op.create_table(
        'traders',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_api_user_id', sa.String(length=36), nullable=True),
        sa.Column('external_reference', sa.String(length=120), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('first_name', sa.String(length=120), nullable=True),
        sa.Column('last_name', sa.String(length=120), nullable=True),
        sa.Column('max_active_leaders', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(['owner_api_user_id'], ['api_users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_traders_external_reference', 'traders', ['external_reference'], unique=True)
    op.create_index('ix_traders_owner_api_user_id', 'traders', ['owner_api_user_id'], unique=False)

    op.create_table(
        'ledger_accounts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('trader_id', sa.String(length=36), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column('balance', sa.Numeric(precision=20, scale=8), nullable=False, server_default=sa.text("0")),
        sa.Column('pending_debit', sa.Numeric(precision=20, scale=8), nullable=False, server_default=sa.text("0")),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('MASTER_ACCOUNT','EXTERNAL_FUNDING','TRADER_HOLDINGS','PF_PAYABLE')", name='ck_ledger_accounts_kind'),
        sa.ForeignKeyConstraint(['trader_id'], ['traders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ledger_accounts_code', 'ledger_accounts', ['code'], unique=False)
    op.create_index('uq_ledger_accounts_global_code', 'ledger_accounts', ['code'], unique=True, postgresql_where=sa.text('trader_id IS NULL'))
    op.create_index('uq_ledger_accounts_trader_holdings', 'ledger_accounts', ['trader_id'], unique=True, postgresql_where=sa.text('trader_id IS NOT NULL'))

    op.create_table(
        'ledger_transactions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('idempotency_key', sa.String(length=120), nullable=False),
        sa.Column('trader_id', sa.String(length=36), nullable=True),
        sa.Column('amount', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('TRADER_DEPOSIT','TRADER_WITHDRAWAL','MASTER_ACCOUNT_FUNDING','PERF_FEE')", name='ck_ledger_tx_kind'),
        sa.CheckConstraint("status IN ('PENDING','POSTED','FAILED')", name='ck_ledger_tx_status'),
        sa.ForeignKeyConstraint(['trader_id'], ['traders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ledger_transactions_idempotency_key', 'ledger_transactions', ['idempotency_key'], unique=True)
    op.create_index('ix_ledger_transactions_kind', 'ledger_transactions', ['kind'], unique=False)
    op.create_index('ix_ledger_transactions_status', 'ledger_transactions', ['status'], unique=False)

    op.create_table(
        'mam_accounts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('trader_id', sa.String(length=36), nullable=True),
        sa.Column('mt5_login', sa.String(length=40), nullable=False),
        sa.Column('provider_account_id', sa.BigInteger(), nullable=True),
        sa.Column('can_be_leader', sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column('can_be_follower', sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column('name', sa.String(length=160), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column('account_mode', sa.String(length=20), nullable=False, server_default=sa.text("'HEDGING'")),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column('platform_group', sa.String(length=120), nullable=True),
        sa.Column('leverage', sa.Integer(), nullable=True),
        sa.Column('rights', sa.Integer(), nullable=True),
        sa.Column('mt5_server', sa.String(length=80), nullable=True),
        sa.Column('mt5_password_enc', sa.LargeBinary(), nullable=True),
        sa.Column('mt5_investor_password_enc', sa.LargeBinary(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint('leverage IS NULL OR leverage > 0', name='ck_mam_accounts_leverage'),
        sa.CheckConstraint("account_mode IN ('HEDGING','NETTING')", name='ck_mam_accounts_mode'),
        sa.CheckConstraint('rights IS NULL OR rights IN (9073, 8981)', name='ck_mam_accounts_rights'),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE','DELETED')", name='ck_mam_accounts_status'),
        sa.ForeignKeyConstraint(['trader_id'], ['traders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mam_accounts_can_be_follower', 'mam_accounts', ['can_be_follower'], unique=False)
    op.create_index('ix_mam_accounts_can_be_leader', 'mam_accounts', ['can_be_leader'], unique=False)
    op.create_index('ix_mam_accounts_capabilities', 'mam_accounts', ['can_be_leader', 'can_be_follower', 'status'], unique=False)
    op.create_index('ix_mam_accounts_mt5_login', 'mam_accounts', ['mt5_login'], unique=True)
    op.create_index('ix_mam_accounts_status', 'mam_accounts', ['status'], unique=False)
    op.create_index('ix_mam_accounts_trader_id', 'mam_accounts', ['trader_id'], unique=False)
    op.create_index('ix_mam_accounts_trader_status', 'mam_accounts', ['trader_id', 'status'], unique=False)

    op.create_table(
        'mam_fee_config_changes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('target_kind', sa.String(length=20), nullable=False),
        sa.Column('target_ref', sa.String(length=60), nullable=False),
        sa.Column('trader_id', sa.String(length=36), nullable=True),
        sa.Column('changed_by_api_user_id', sa.String(length=36), nullable=True),
        sa.Column('previous_rate', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('previous_enabled', sa.Boolean(), nullable=True),
        sa.Column('previous_period', sa.String(length=20), nullable=True),
        sa.Column('new_rate', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('new_enabled', sa.Boolean(), nullable=True),
        sa.Column('new_period', sa.String(length=20), nullable=True),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint('new_rate IS NULL OR (new_rate >= 0 AND new_rate <= 1)', name='ck_mam_fee_changes_rate'),
        sa.CheckConstraint("target_kind IN ('LEADER_PROFILE','ALLOCATION')", name='ck_mam_fee_changes_target'),
        sa.ForeignKeyConstraint(['trader_id'], ['traders.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['changed_by_api_user_id'], ['api_users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mam_fee_changes_target_created', 'mam_fee_config_changes', ['target_kind', 'target_ref', 'created_at'], unique=False)
    op.create_index('ix_mam_fee_config_changes_changed_by_api_user_id', 'mam_fee_config_changes', ['changed_by_api_user_id'], unique=False)
    op.create_index('ix_mam_fee_config_changes_target_kind', 'mam_fee_config_changes', ['target_kind'], unique=False)
    op.create_index('ix_mam_fee_config_changes_target_ref', 'mam_fee_config_changes', ['target_ref'], unique=False)
    op.create_index('ix_mam_fee_config_changes_trader_id', 'mam_fee_config_changes', ['trader_id'], unique=False)

    op.create_table(
        'mam_perf_fee_payments',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('provider_payment_id', sa.BigInteger(), nullable=False),
        sa.Column('run_id', sa.BigInteger(), nullable=True),
        sa.Column('master_login', sa.String(length=40), nullable=False),
        sa.Column('payment_account_login', sa.String(length=40), nullable=True),
        sa.Column('investor_mt5_login', sa.String(length=40), nullable=True),
        sa.Column('allocation_id', sa.BigInteger(), nullable=True),
        sa.Column('trader_id', sa.String(length=36), nullable=True),
        sa.Column('amount', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column('status', sa.String(length=30), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ledger_tx_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint('amount >= 0', name='ck_mam_perf_fee_payments_amount'),
        sa.ForeignKeyConstraint(['trader_id'], ['traders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mam_perf_fee_payments_allocation_id', 'mam_perf_fee_payments', ['allocation_id'], unique=False)
    op.create_index('ix_mam_perf_fee_payments_investor_mt5_login', 'mam_perf_fee_payments', ['investor_mt5_login'], unique=False)
    op.create_index('ix_mam_perf_fee_payments_master_executed', 'mam_perf_fee_payments', ['master_login', 'executed_at'], unique=False)
    op.create_index('ix_mam_perf_fee_payments_master_login', 'mam_perf_fee_payments', ['master_login'], unique=False)
    op.create_index('ix_mam_perf_fee_payments_provider_payment_id', 'mam_perf_fee_payments', ['provider_payment_id'], unique=True)
    op.create_index('ix_mam_perf_fee_payments_run', 'mam_perf_fee_payments', ['run_id', 'investor_mt5_login'], unique=False)
    op.create_index('ix_mam_perf_fee_payments_run_id', 'mam_perf_fee_payments', ['run_id'], unique=False)
    op.create_index('ix_mam_perf_fee_payments_trader_id', 'mam_perf_fee_payments', ['trader_id'], unique=False)

    op.create_table(
        'crypto_deposits',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('api_user_id', sa.String(length=36), nullable=True),
        sa.Column('tx_hash', sa.String(length=66), nullable=False),
        sa.Column('chain_id', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('declared_amount', sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column('onchain_amount', sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column('raw_amount', sa.String(length=80), nullable=True),
        sa.Column('token_symbol', sa.String(length=20), nullable=False, server_default=sa.text("'USDC'")),
        sa.Column('token_contract', sa.String(length=42), nullable=True),
        sa.Column('token_decimals', sa.BigInteger(), nullable=True),
        sa.Column('from_address', sa.String(length=42), nullable=True),
        sa.Column('to_address', sa.String(length=42), nullable=True),
        sa.Column('block_number', sa.BigInteger(), nullable=True),
        sa.Column('confirmations', sa.BigInteger(), nullable=True),
        sa.Column('rejection_code', sa.String(length=40), nullable=True),
        sa.Column('rejection_detail', sa.String(length=500), nullable=True),
        sa.Column('ledger_tx_id', sa.String(length=36), nullable=True),
        sa.Column('notified_admins', sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint('declared_amount > 0', name='ck_crypto_deposits_declared_amount'),
        sa.CheckConstraint("status IN ('CONFIRMED','REJECTED')", name='ck_crypto_deposits_status'),
        sa.ForeignKeyConstraint(['ledger_tx_id'], ['ledger_transactions.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['api_user_id'], ['api_users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_crypto_deposits_api_user_id', 'crypto_deposits', ['api_user_id'], unique=False)
    op.create_index('ix_crypto_deposits_created', 'crypto_deposits', ['created_at'], unique=False)
    op.create_index('ix_crypto_deposits_from_address', 'crypto_deposits', ['from_address'], unique=False)
    op.create_index('ix_crypto_deposits_ledger_tx_id', 'crypto_deposits', ['ledger_tx_id'], unique=False)
    op.create_index('ix_crypto_deposits_status', 'crypto_deposits', ['status'], unique=False)
    op.create_index('ix_crypto_deposits_tx_hash', 'crypto_deposits', ['tx_hash'], unique=False)
    op.create_index('uq_crypto_deposits_confirmed_tx', 'crypto_deposits', ['tx_hash'], unique=True, postgresql_where=sa.text("status = 'CONFIRMED'"))

    op.create_table(
        'ledger_entries',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tx_id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('debit', sa.Numeric(precision=20, scale=8), nullable=False, server_default=sa.text("0")),
        sa.Column('credit', sa.Numeric(precision=20, scale=8), nullable=False, server_default=sa.text("0")),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(['tx_id'], ['ledger_transactions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['ledger_accounts.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ledger_entries_account_id', 'ledger_entries', ['account_id'], unique=False)
    op.create_index('ix_ledger_entries_tx_id', 'ledger_entries', ['tx_id'], unique=False)

    op.create_table(
        'mam_allocations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('allocation_id', sa.BigInteger(), nullable=True),
        sa.Column('leader_account_id', sa.String(length=36), nullable=False),
        sa.Column('follower_account_id', sa.String(length=36), nullable=False),
        sa.Column('leader_login', sa.String(length=40), nullable=False),
        sa.Column('follower_login', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'PAUSED'")),
        sa.Column('allocation_mode', sa.String(length=30), nullable=False, server_default=sa.text("'EQUITY'")),
        sa.Column('mode_parameter', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('equity_stop', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('unsubscribe_policy', sa.String(length=30), nullable=False, server_default=sa.text("'CLOSE_ON_UNSUBSCRIBE'")),
        sa.Column('performance_fee_rate', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('performance_fee_enabled', sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column('max_active_leaders_requested', sa.Integer(), nullable=True),
        sa.Column('terminated_reason', sa.String(length=40), nullable=True),
        sa.Column('terminated_by', sa.String(length=20), nullable=True),
        sa.Column('performance_fee_charged', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_polled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_detail', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint('equity_stop IS NULL OR equity_stop >= 0', name='ck_mam_allocations_equity_stop'),
        sa.CheckConstraint('max_active_leaders_requested IS NULL OR max_active_leaders_requested >= 0', name='ck_mam_allocations_max_leaders'),
        sa.CheckConstraint("allocation_mode IN ('FIXED','SCALED','EQUITY','EQUITY_ROUND_DOWN','BALANCE')", name='ck_mam_allocations_mode'),
        sa.CheckConstraint('mode_parameter IS NULL OR mode_parameter > 0', name='ck_mam_allocations_mode_param_positive'),
        sa.CheckConstraint("allocation_mode NOT IN ('FIXED','SCALED') OR mode_parameter IS NOT NULL", name='ck_mam_allocations_mode_param_required'),
        sa.CheckConstraint('leader_login <> follower_login', name='ck_mam_allocations_not_self'),
        sa.CheckConstraint('leader_account_id <> follower_account_id', name='ck_mam_allocations_not_self_id'),
        sa.CheckConstraint("unsubscribe_policy IN ('KEEP_OPEN','CLOSE_ON_UNSUBSCRIBE')", name='ck_mam_allocations_policy'),
        sa.CheckConstraint('performance_fee_rate IS NULL OR (performance_fee_rate >= 0 AND performance_fee_rate <= 1)', name='ck_mam_allocations_rate'),
        sa.CheckConstraint("status IN ('PAUSED','ACTIVE','STOPPING','CANCELLED','ERROR')", name='ck_mam_allocations_status'),
        sa.ForeignKeyConstraint(['follower_account_id'], ['mam_accounts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['leader_account_id'], ['mam_accounts.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mam_allocations_allocation_id', 'mam_allocations', ['allocation_id'], unique=True)
    op.create_index('ix_mam_allocations_follower_account_id', 'mam_allocations', ['follower_account_id'], unique=False)
    op.create_index('ix_mam_allocations_follower_login', 'mam_allocations', ['follower_login'], unique=False)
    op.create_index('ix_mam_allocations_follower_status', 'mam_allocations', ['follower_login', 'status'], unique=False)
    op.create_index('ix_mam_allocations_leader_account_id', 'mam_allocations', ['leader_account_id'], unique=False)
    op.create_index('ix_mam_allocations_leader_login', 'mam_allocations', ['leader_login'], unique=False)
    op.create_index('ix_mam_allocations_status', 'mam_allocations', ['status'], unique=False)
    op.create_index('ix_mam_allocations_status_polled', 'mam_allocations', ['status', 'last_polled_at'], unique=False)
    op.create_index('uq_mam_allocations_live_pair', 'mam_allocations', ['leader_login', 'follower_login'], unique=True, postgresql_where=sa.text("status IN ('ACTIVE','PAUSED','STOPPING')"))

    op.create_table(
        'mam_leader_profiles',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('leader_id', sa.BigInteger(), nullable=True),
        sa.Column('account_login', sa.String(length=40), nullable=False),
        sa.Column('payment_account_login', sa.String(length=40), nullable=True),
        sa.Column('strategy_name', sa.String(length=160), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('leaderboard_visibility', sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column('restrict_simultaneous_connections', sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column('min_deposit', sa.Numeric(precision=20, scale=8), nullable=False, server_default=sa.text("0")),
        sa.Column('performance_fee_rate', sa.Numeric(precision=9, scale=6), nullable=False, server_default=sa.text("0")),
        sa.Column('performance_fee_period', sa.String(length=20), nullable=False, server_default=sa.text("'MONTHLY'")),
        sa.Column('propagation_mode', sa.String(length=20), nullable=False, server_default=sa.text("'ORIGINAL_ONLY'")),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("performance_fee_period IN ('OFF','MINUTELY','HOURLY','DAILY','WEEKLY','SEMIMONTHLY','MONTHLY')", name='ck_mam_leader_fee_period'),
        sa.CheckConstraint('min_deposit >= 0', name='ck_mam_leader_min_deposit'),
        sa.CheckConstraint('payment_account_login IS NULL OR payment_account_login <> account_login', name='ck_mam_leader_payment_distinct'),
        sa.CheckConstraint("propagation_mode IN ('ORIGINAL_ONLY','CASCADE')", name='ck_mam_leader_propagation'),
        sa.CheckConstraint('performance_fee_rate >= 0 AND performance_fee_rate <= 1', name='ck_mam_leader_rate'),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name='ck_mam_leader_status'),
        sa.ForeignKeyConstraint(['account_id'], ['mam_accounts.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mam_leader_profiles_account_id', 'mam_leader_profiles', ['account_id'], unique=True)
    op.create_index('ix_mam_leader_profiles_account_login', 'mam_leader_profiles', ['account_login'], unique=True)
    op.create_index('ix_mam_leader_profiles_leader_id', 'mam_leader_profiles', ['leader_id'], unique=False)
    op.create_index('ix_mam_leader_profiles_status', 'mam_leader_profiles', ['status'], unique=False)
    op.create_index('uq_mam_leader_payment_login', 'mam_leader_profiles', ['payment_account_login'], unique=True, postgresql_where=sa.text('payment_account_login IS NOT NULL'))

    op.create_table(
        'movements',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('trader_id', sa.String(length=36), nullable=True),
        sa.Column('direction', sa.String(length=20), nullable=False),
        sa.Column('amount', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default=sa.text("'USD'")),
        sa.Column('idempotency_key', sa.String(length=120), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('mam_account_id', sa.String(length=36), nullable=True),
        sa.Column('mt5_login', sa.String(length=40), nullable=True),
        sa.Column('allocation_id', sa.BigInteger(), nullable=True),
        sa.Column('crm_reference', sa.String(length=120), nullable=True),
        sa.Column('requested_amount', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('effective_amount', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('perf_fee_at_request', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('balance_before', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('balance_after', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('mt5_deal_id', sa.BigInteger(), nullable=True),
        sa.Column('resolved_by_api_user_id', sa.String(length=36), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_note', sa.String(length=500), nullable=True),
        sa.Column('provider_result', sa.String(length=30), nullable=True),
        sa.Column('provider_reference', sa.String(length=64), nullable=True),
        sa.Column('ledger_tx_id', sa.String(length=36), nullable=True),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('error_detail', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("direction IN ('DEPOSIT','WITHDRAWAL','FUNDING','SUBSCRIBE','UNSUBSCRIBE','PERF_FEE','PAYMENT_WITHDRAWAL')", name='ck_movements_direction'),
        sa.CheckConstraint("status IN ('PENDING','COMPLETED','FAILED','AMBIGUOUS','REJECTED')", name='ck_movements_status'),
        sa.ForeignKeyConstraint(['mam_account_id'], ['mam_accounts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['resolved_by_api_user_id'], ['api_users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['trader_id'], ['traders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_movements_allocation_id', 'movements', ['allocation_id'], unique=False)
    op.create_index('ix_movements_crm_reference', 'movements', ['crm_reference'], unique=True)
    op.create_index('ix_movements_idempotency_key', 'movements', ['idempotency_key'], unique=True)
    op.create_index('ix_movements_mam_account_id', 'movements', ['mam_account_id'], unique=False)
    op.create_index('ix_movements_mt5_deal_id', 'movements', ['mt5_deal_id'], unique=False)
    op.create_index('ix_movements_mt5_login', 'movements', ['mt5_login'], unique=False)
    op.create_index('ix_movements_status', 'movements', ['status'], unique=False)
    op.create_index('ix_movements_status_direction', 'movements', ['status', 'direction'], unique=False)
    op.create_index('ix_movements_trader_created', 'movements', ['trader_id', 'created_at'], unique=False)
    op.create_index('ix_movements_trader_id', 'movements', ['trader_id'], unique=False)
    op.create_index('uq_movements_one_pending_per_account', 'movements', ['mam_account_id'], unique=True, postgresql_where=sa.text("status = 'PENDING' AND mam_account_id IS NOT NULL"))

    # ── seed: cuentas globales del plan contable ──
    # Sin estas filas el ledger no puede postear nada: son los dos extremos del
    # asiento de fondeo y la contrapartida de los performance fees cedidos.
    op.bulk_insert(
        sa.table(
            "ledger_accounts",
            sa.column("id", sa.String), sa.column("code", sa.String),
            sa.column("kind", sa.String), sa.column("currency", sa.String),
        ),
        [
            # De aca sale y entra todo el capital que se reparte a los clientes.
            {"id": str(uuid.uuid4()), "code": "MASTER_ACCOUNT",
             "kind": "MASTER_ACCOUNT", "currency": "USD"},
            # Contrapartida del fondeo externo (manual con OTP o deposito on-chain).
            {"id": str(uuid.uuid4()), "code": "EXTERNAL_FUNDING",
             "kind": "EXTERNAL_FUNDING", "currency": "USD"},
            # Acumulado de fees cedidos al leader. NO es plata nuestra, por eso
            # no toca la cuenta maestra.
            {"id": str(uuid.uuid4()), "code": "PERF_FEE_PAID",
             "kind": "PF_PAYABLE", "currency": "USD"},
        ],
    )


def downgrade() -> None:

    op.drop_table('movements')
    op.drop_table('mam_leader_profiles')
    op.drop_table('mam_allocations')
    op.drop_table('ledger_entries')
    op.drop_table('crypto_deposits')
    op.drop_table('mam_perf_fee_payments')
    op.drop_table('mam_fee_config_changes')
    op.drop_table('mam_accounts')
    op.drop_table('ledger_transactions')
    op.drop_table('ledger_accounts')
    op.drop_table('traders')
    op.drop_table('mam_deletion_operations')
    op.drop_table('api_keys')
    op.drop_table('mam_webhook_events')
    op.drop_table('funding_otps')
    op.drop_table('api_users')
