"""Router agregado de la API v1. Todos los endpoints requieren API key.

Roles: lo ÚNICO exclusivo de ADMIN es fondear la cuenta maestra (endpoints de
funding, gateados dentro de master_account.py) y la gestión de api_users/keys
(admin.router) — porque abrirla dejaría que cualquier USER se creara un ADMIN y
fondeara, rompiendo justo esa restricción. Todo lo demás lo puede hacer cualquier
api_user autenticado (USER o ADMIN). Los clientes y sus cuentas siguen scopeados
por dueño.

El receptor del webhook del motor MAM NO cuelga de acá: se monta aparte en
main.py porque el proveedor autentica con firma HMAC, no con nuestra API key
(spec §11.6).
"""
from fastapi import APIRouter, Depends

from app.api.deps import require_admin, require_api_key
from app.api.v1 import (
    admin,
    crypto_deposits,
    mam,
    master_account,
    movements,
    reports,
    traders,
)

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
api_router.include_router(traders.router)
api_router.include_router(mam.router)
api_router.include_router(crypto_deposits.router)
api_router.include_router(movements.router)
api_router.include_router(master_account.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router, dependencies=[Depends(require_admin)])
