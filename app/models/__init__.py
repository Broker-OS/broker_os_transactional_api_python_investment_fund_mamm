"""Registro central de modelos (para Alembic: importa todos → Base.metadata)."""
from app.models.api_key import ApiKey  # noqa: F401
from app.models.api_user import ApiUser  # noqa: F401
from app.models.crypto_deposit import CryptoDeposit  # noqa: F401
from app.models.funding_otp import FundingOtp  # noqa: F401
from app.models.ledger import (  # noqa: F401
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
)
from app.models.mam import (  # noqa: F401
    MamAccount,
    MamAllocation,
    MamDeletionOperation,
    MamFeeConfigChange,
    MamLeaderProfile,
    MamPerfFeePayment,
    MamWebhookEvent,
)
from app.models.movement import Movement  # noqa: F401
from app.models.trader import Trader  # noqa: F401
