"""Crea un api_user y su API key. La key se imprime UNA sola vez (en BD va el hash).

Uso:
    python -m scripts.create_api_key "Nombre" email@dominio.com [ADMIN|USER]

Una API key sin api_user asociado NO sirve: la autenticacion resuelve la key
hasta su dueño para saber que rol tiene, y si no lo encuentra la rechaza. Por eso
este script crea (o reusa) el usuario y despues le cuelga la credencial.

Si el email ya existe se reusa ese usuario y solo se emite una key nueva: asi
rotar una credencial no duplica identidades.
"""
import asyncio
import sys

from sqlalchemy import select

from app.core.security import generate_api_key
from app.db.database import AsyncSessionLocal
from app.models import ApiKey, ApiUser
from app.models.api_user import ROLES


async def main(name: str, email: str, role: str) -> None:
    if role not in ROLES:
        sys.exit(f"Rol invalido: {role!r}. Valores admitidos: {', '.join(ROLES)}")

    full_key, key_prefix, key_hash = generate_api_key()
    async with AsyncSessionLocal() as db:
        found = await db.execute(select(ApiUser).where(ApiUser.email == email))
        user = found.scalar_one_or_none()
        if user is None:
            user = ApiUser(email=email, name=name, role=role)
            db.add(user)
            await db.flush()
            creado = "creado"
        else:
            creado = f"ya existia (rol {user.role})"
        db.add(ApiKey(api_user_id=user.id, name=name,
                      key_prefix=key_prefix, key_hash=key_hash))
        await db.commit()
        user_id, user_role = user.id, user.role

    print("=== API key creada (GUARDALA, se muestra UNA sola vez) ===")
    print(f"api_user : {name} <{email}>  [{creado}]")
    print(f"rol      : {user_role}")
    print(f"id       : {user_id}")
    print(f"prefix   : {key_prefix}")
    print(f"API KEY  : {full_key}")
    print("Enviala en el header:  X-API-Key: <API KEY>")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2],
                     sys.argv[3].upper() if len(sys.argv) > 3 else "USER"))
