"""
Corre toda la suite de verificacion del motor MAM.

    python tests/run_all.py            # solo las suites que NO necesitan BD
    python tests/run_all.py --with-db  # tambien las de BD (DESTRUCTIVO)

Las suites `test_db_*` recrean el esquema entre corridas, asi que necesitan una
BASE DEDICADA. Por seguridad se exige `TEST_DATABASE_URL` y que el nombre de la
base contenga "test": nunca se toca la BD que use la app.

    export TEST_DATABASE_URL="postgresql+asyncpg://postgres:pass@127.0.0.1:5432/brokeros_mam_test"
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
PY = sys.executable

# Las suites del motor MAM (cuentas, perfiles de leader, allocations, webhook)
# se van sumando a medida que se implementan los servicios.
SIN_BD = [
    "test_mam_client.py",
    "test_webhook_signature.py",
]
CON_BD = [
    "test_db_perf_fee.py",
    "test_db_crypto_deposits.py",
]


def _test_db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        sys.exit(
            "Falta TEST_DATABASE_URL. Las suites de BD RECREAN el esquema: apuntalas a una\n"
            "base dedicada, nunca a la de la app.\n"
            '  export TEST_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/brokeros_mam_test"'
        )
    nombre = url.rsplit("/", 1)[-1].split("?")[0].lower()
    if "test" not in nombre:
        sys.exit(
            f"La base '{nombre}' no parece de pruebas (su nombre no contiene 'test').\n"
            "Se aborta para no destruir datos reales."
        )
    return url


def _reset_schema(url: str) -> None:
    """Deja el esquema vacio y vuelve a migrar."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_run())
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([PY, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                   check=True, capture_output=True)


def _run(script: str, env: dict) -> tuple[str, int]:
    p = subprocess.run([PY, str(TESTS / script)], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    salida = (p.stdout or "") + (p.stderr or "")
    fallos = -1
    for linea in salida.splitlines():
        if linea.startswith("FALLOS:"):
            fallos = int(linea.split(":")[1].strip())
    if fallos < 0:
        print(salida[-2000:])
    return salida, fallos


def main() -> int:
    con_bd = "--with-db" in sys.argv
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    total = 0

    print("== Suites sin base de datos ==")
    for s in SIN_BD:
        _, fallos = _run(s, env)
        print(f"  {s:<34} {'OK' if fallos == 0 else f'FALLOS: {fallos}'}")
        total += max(fallos, 1) if fallos != 0 else 0

    if con_bd:
        url = _test_db_url()
        env = {**env, "DATABASE_URL": url}
        print(f"\n== Suites con base de datos ({url.rsplit('/', 1)[-1]}) ==")
        for s in CON_BD:
            _reset_schema(url)
            _, fallos = _run(s, env)
            print(f"  {s:<34} {'OK' if fallos == 0 else f'FALLOS: {fallos}'}")
            total += max(fallos, 1) if fallos != 0 else 0
    else:
        print("\n(Suites de BD omitidas: correr con --with-db y TEST_DATABASE_URL)")

    print("\n" + "=" * 56)
    print("RESULTADO:", "TODO VERDE" if total == 0 else f"{total} fallo(s)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
