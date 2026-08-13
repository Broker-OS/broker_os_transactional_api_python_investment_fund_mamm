"""Crea una API key para el socio. La imprime UNA sola vez (en BD solo va el hash).

Uso:  python -m scripts.create_api_key "Nombre del socio"
"""
import asyncio
import sys

from app.core.security import generate_api_key
from app.db.database import AsyncSessionLocal
from app.models import ApiKey


async def main(name: str) -> None:
    full_key, key_prefix, key_hash = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(name=name, key_prefix=key_prefix, key_hash=key_hash))
        await db.commit()
    print("=== API key creada (GUARDALA, se muestra UNA sola vez) ===")
    print(f"nombre : {name}")
    print(f"prefix : {key_prefix}")
    print(f"API KEY: {full_key}")
    print("Enviala en el header:  X-API-Key: <API KEY>")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "socio"
    asyncio.run(main(name))
