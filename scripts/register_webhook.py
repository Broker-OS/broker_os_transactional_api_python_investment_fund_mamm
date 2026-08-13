"""Registra el webhook de terminacion contra el motor MAM. OPERACION DE UN SOLO TIRO.

    python -m scripts.register_webhook            # registra y guarda el secreto en .env
    python -m scripts.register_webhook --dry-run  # solo muestra que se va a mandar

Spec §11.6:

  * El motor admite UN SOLO destino. Un segundo registro devuelve 409.
  * No genera eventos retroactivos: hay que registrarlo ANTES de las
    terminaciones que se quieren recibir.
  * El `signing_secret` se entrega EN TEXTO PLANO UNA SOLA VEZ. No se puede
    volver a consultar.

Por eso el script escribe el secreto en el `.env` en el mismo acto: pedirle al
operador que lo copie de la pantalla es la forma mas facil de perderlo para
siempre. El valor NO se imprime completo.

Despues de correrlo hay que reiniciar el servicio para que tome el secreto.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import settings  # noqa: E402
from app.services.mam_client import get_mam_client  # noqa: E402

ENV_PATH = os.path.join(ROOT, ".env")
CLAVE = "MAM_WEBHOOK_SIGNING_SECRET"


def _guardar_secreto(secreto: str) -> None:
    """Escribe el secreto en el .env, respetando el resto del archivo."""
    lineas = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8", errors="replace") as f:
            lineas = f.read().splitlines()

    reemplazado = False
    for i, linea in enumerate(lineas):
        if linea.strip().startswith(f"{CLAVE}="):
            lineas[i] = f"{CLAVE}={secreto}"
            reemplazado = True
            break
    if not reemplazado:
        lineas.append(f"{CLAVE}={secreto}")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")
    os.chmod(ENV_PATH, 0o600)


async def main(dry_run: bool) -> int:
    url = (settings.MAM_WEBHOOK_PUBLIC_URL or "").strip()
    nombre = (settings.MAM_WEBHOOK_NAME or "").strip()

    if not url:
        print("ERROR: falta MAM_WEBHOOK_PUBLIC_URL en el .env")
        return 1
    if not url.startswith("https://"):
        # El payload lleva logins y montos; sobre HTTP viaja en claro.
        print(f"ERROR: la URL debe ser HTTPS. Recibida: {url}")
        return 1

    ya = (settings.MAM_WEBHOOK_SIGNING_SECRET or "").strip()
    if ya and not dry_run:
        print("Ya hay un secreto configurado. El motor admite UN SOLO destino y un\n"
              "segundo registro devuelve 409. Si de verdad hay que re-registrar,\n"
              f"borra {CLAVE} del .env primero y coordina con el proveedor.")
        return 1

    print(f"nombre : {nombre}")
    print(f"url    : {url}")
    if dry_run:
        print("\n(--dry-run: no se registro nada)")
        return 0

    data = await get_mam_client().register_webhook(name=nombre, url=url)
    secreto = (data.get("signing_secret") or "").strip()
    if not secreto:
        print("ERROR: el motor no devolvio signing_secret. Respuesta:", sorted(data))
        return 1

    _guardar_secreto(secreto)
    print("\n=== Webhook registrado ===")
    print(f"id        : {data.get('id')}")
    print(f"estado    : {data.get('status')}")
    print(f"algoritmo : {data.get('signature_algorithm')}")
    print(f"secreto   : {secreto[:4]}…{secreto[-4:]}  ({len(secreto)} chars) → guardado en .env")
    print("\nReiniciar el servicio para que lo tome:")
    print("  sudo systemctl restart broker-os-mam")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--dry-run" in sys.argv)))
