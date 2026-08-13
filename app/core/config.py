"""Configuracion del servicio (pydantic-settings). Todo secreto vive en .env."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # App
    APP_NAME: str = "Broker OS · Investment Fund MAM"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = Field(default="development")
    DEBUG: bool = Field(default=True)
    HTTP_PORT: int = Field(default=8200)
    ROOT_PATH: str = Field(default="")

    # Base de datos (asyncpg). BD PROPIA: este servicio no comparte esquema con
    # el bridge PAMM aunque el modelo contable sea el mismo.
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/brokeros_mam"
    )

    # ─────────── MAM API (proveedor externo) ───────────
    # Spec §3.1/§3.2. La API key es de USO EXCLUSIVO DEL BACKEND: nunca al
    # navegador, ni a una app movil, ni a un repositorio.
    MAM_API_BASE_URL: str = Field(default="")
    MAM_API_KEY: str = Field(default="")

    # Timeouts diferenciados por tipo de endpoint: las operaciones que tocan MT5
    # son bastante mas lentas que una consulta.
    MAM_TIMEOUT_READ_SECONDS: float = Field(default=15.0)
    MAM_TIMEOUT_FINANCIAL_SECONDS: float = Field(default=60.0)
    MAM_TIMEOUT_PROVISION_SECONDS: float = Field(default=90.0)

    # Paginacion por cursor (spec §3.6). El tope de paginas evita barrer un
    # listado completo sin querer.
    MAM_PAGE_SIZE: int = Field(default=100, ge=1, le=100)
    MAM_MAX_PAGES: int = Field(default=50, ge=1)

    # ─────────── Provisioning MT5 (spec §5, paso 1) ───────────
    # Grupo MT5 real asignado por el broker. SIN ESTE DATO NO SE PUEDEN CREAR
    # CUENTAS: los endpoints de provisioning fallan de forma explicita.
    MAM_MT5_PLATFORM_GROUP: str = Field(default="")
    MAM_MT5_DEFAULT_LEVERAGE: int = Field(default=100, ge=1)
    # Nombre del servidor MT5 que se guarda en cada cuenta creada (3er dato que
    # el cliente necesita para entrar a la terminal).
    MAM_MT5_SERVER: str = Field(default="")

    # Mascara de permisos MT5 (spec §5 paso 1 y §11.1). La integracion soporta
    # DOS perfiles y nada mas:
    #   9073 (0x2371) -> trading habilitado (base MAM + trailing stop + EAs)
    #   8981 (0x2315) -> trading deshabilitado (base MAM + flag Trade Disabled)
    # Si se omite el campo, el servicio del proveedor usa 1 (0x1), que NO es
    # ninguno de los dos: por eso se manda siempre explicito.
    MAM_MT5_RIGHTS_TRADING_ENABLED: int = Field(default=9073, ge=0)
    MAM_MT5_RIGHTS_TRADING_DISABLED: int = Field(default=8981, ge=0)

    # Spec §4.1: para participar en copy trading la cuenta DEBE usar HEDGING.
    MAM_ACCOUNT_MODE: str = Field(default="HEDGING")

    # ─────────── Limites de producto (spec §7.1) ───────────
    # `max_active_leaders_per_follower` NO lo guarda el MAM API: lo resuelve el
    # integrador desde su propio plan y lo manda en CADA POST de allocation.
    # Este es el valor por defecto cuando el plan del trader no dice otra cosa.
    MAM_DEFAULT_MAX_ACTIVE_LEADERS: int = Field(default=1, ge=0)

    # ─────────── Webhook de terminacion de allocations (spec §11.6) ───────────
    # `signing_secret` se entrega EN CLARO UNA SOLA VEZ al registrar el webhook.
    # Guardarlo aca es lo que permite validar X-MAM-Signature.
    MAM_WEBHOOK_SIGNING_SECRET: str = Field(default="")
    # URL publica HTTPS que se registra en el proveedor (POST /api/v1/mam/webhooks).
    MAM_WEBHOOK_PUBLIC_URL: str = Field(default="")
    MAM_WEBHOOK_NAME: str = Field(default="broker-os-crm")
    # Spec §11.6: "rechace timestamps con mas de cinco minutos".
    MAM_WEBHOOK_MAX_SKEW_SECONDS: int = Field(default=300, ge=30)

    # Clave Fernet para cifrar las credenciales MT5 en reposo. Generar con:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    MT5_CREDENTIALS_ENCRYPTION_KEY: str = Field(default="")

    # ─────────── Depositos on-chain (EVM) ───────────
    # Verificacion de pagos en USDC sobre una cadena EVM. Por defecto BNB Smart
    # Chain TESTNET (chain id 97).
    EVM_RPC_URL: str = Field(default="https://data-seed-prebsc-1-s1.bnbchain.org:8545")
    EVM_CHAIN_ID: int = Field(default=97)
    EVM_NETWORK_NAME: str = Field(default="BSC Testnet")

    # Address NUESTRA que recibe el dinero. Toda transferencia se valida contra
    # esta direccion: si el token no llego aca, el deposito no cuenta.
    EVM_RECEIVING_ADDRESS: str = Field(default="")

    # Contrato del token USDC en esa cadena. En BSC testnet NO hay un USDC
    # canonico: es el contrato que use el proyecto, hay que cargarlo explicito.
    EVM_USDC_CONTRACT: str = Field(default="")
    EVM_TOKEN_SYMBOL: str = Field(default="USDC")
    # OJO: en BSC los tokens suelen tener 18 decimales; el USDC de Ethereum tiene 6.
    # Un valor equivocado hace que los montos se lean mal por 12 ordenes de magnitud.
    EVM_TOKEN_DECIMALS: int = Field(default=18, ge=0, le=36)

    # Confirmaciones minimas antes de dar por buena una transaccion (reorgs).
    EVM_MIN_CONFIRMATIONS: int = Field(default=12, ge=0)
    EVM_HTTP_TIMEOUT_SECONDS: float = Field(default=20.0)

    # Un deposito verificado acredita la cuenta maestra en el mismo commit
    # (asiento MASTER_ACCOUNT_FUNDING). Apagarlo deja el deposito registrado pero
    # sin asiento: sirve para revisar a mano antes de acreditar, sin desplegar.
    #
    # ⚠️ Da por sentado que 1 token = 1 unidad de LEDGER_CURRENCY. Vale para
    #    stablecoins ancladas al dolar (USDC, USDT). Con un token que flote,
    #    el ledger quedaria mal: habria que convertir a la cotizacion del dia.
    CRYPTO_DEPOSIT_AUTO_CREDIT: bool = Field(default=True)

    # Contabilidad
    LEDGER_CURRENCY: str = Field(default="USD")

    # ─────────── Email para OTP ───────────
    # Transporte preferido: AWS SES (mismas credenciales que BrokerOS). Si SES no
    # esta configurado se intenta SMTP; si tampoco, el envio queda DESHABILITADO y
    # el endpoint de OTP devuelve el codigo en la respuesta (modo dev).
    AWS_ACCESS_KEY_ID: str = Field(default="")
    AWS_SECRET_ACCESS_KEY: str = Field(default="")
    AWS_REGION: str = Field(default="us-east-1")
    AWS_SES_SENDER_EMAIL: str = Field(default="")

    # Alternativa SMTP (solo se usa si SES no esta configurado).
    SMTP_HOST: str = Field(default="")
    SMTP_PORT: int = Field(default=587)
    SMTP_USER: str = Field(default="")
    SMTP_PASSWORD: str = Field(default="")
    SMTP_FROM: str = Field(default="")
    SMTP_USE_TLS: bool = Field(default=True)
    # Email por defecto que recibe el OTP de fondeo si el request no envia uno.
    FUNDING_OTP_EMAIL: str = Field(default="")

    # ─────────── OTP (fondeo de la cuenta maestra) ───────────
    OTP_LENGTH: int = Field(default=6, ge=4, le=10)
    OTP_EXPIRY_MINUTES: int = Field(default=10, ge=1)
    OTP_MAX_ATTEMPTS: int = Field(default=5, ge=1)


settings = Settings()
