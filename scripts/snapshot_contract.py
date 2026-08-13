"""Congela el contrato del motor MAM para poder detectar cuando cambie.

    python -m scripts.snapshot_contract          # muestra el diff contra el snapshot
    python -m scripts.snapshot_contract --write  # actualiza el snapshot

El proveedor esta en version 0.1.0 y va a cambiar. Sin un snapshot, un campo que
cambia de nombre o un endpoint que desaparece se descubren cuando un cliente no
puede operar. Con esto, el test de drift falla en el pipeline antes de desplegar.

NO se guarda el OpenAPI completo: solo la superficie que consumimos —rutas,
metodos, parametros y campos de request/response de los schemas que tocamos—. Un
snapshot del archivo entero fallaria ante cualquier retoque de una descripcion,
y un test que grita por cosas que no importan se termina ignorando.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import asyncio  # noqa: E402

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402

SNAPSHOT = os.path.join(ROOT, "contracts", "mam-openapi-snapshot.json")

# Las rutas que este servicio consume de verdad.
RUTAS = [
    "/api/v1/mam/accounts",
    "/api/v1/mam/accounts/add",
    "/api/v1/mam/accounts/create",
    "/api/v1/mam/accounts/{account_login}",
    "/api/v1/mam/accounts/{account_login}/metrics",
    "/api/v1/mam/accounts/{account_login}/deposit",
    "/api/v1/mam/accounts/{account_login}/withdraw",
    "/api/v1/mam/accounts/{account_login}/balance-transactions",
    "/api/v1/mam/leaders",
    "/api/v1/mam/leaders/{leader_id}",
    "/api/v1/mam/leaders/{master_login}/payment-account/balance",
    "/api/v1/mam/leaders/{master_login}/payment-account/withdraw",
    "/api/v1/mam/allocations",
    "/api/v1/mam/allocations/subscription-eligibility",
    "/api/v1/mam/allocations/{allocation_id}",
    "/api/v1/mam/allocations/{allocation_id}/unsubscribe",
    "/api/v1/mam/webhooks",
    "/api/v1/perf-fee/transactions",
    "/api/v1/perf-fee/master/{master_login}/investor-payments",
]


def _campos(spec, ref, visto=None):
    """Campos de un schema, siguiendo un nivel de anidamiento."""
    visto = visto or set()
    nombre = ref.split("/")[-1]
    if nombre in visto:
        return []
    visto.add(nombre)
    props = spec.get("components", {}).get("schemas", {}).get(nombre, {}).get("properties", {})
    salida = []
    for k, v in sorted(props.items()):
        salida.append(k)
        hijo = v.get("items", {}).get("$ref")
        if hijo:
            salida += [f"{k}[].{c}" for c in _campos(spec, hijo, visto)]
    return salida


def superficie(spec: dict) -> dict:
    """Extrae solo lo que nos puede romper."""
    out = {}
    for ruta in RUTAS:
        item = spec.get("paths", {}).get(ruta)
        if item is None:
            out[ruta] = {"__falta__": True}
            continue
        for metodo, op in item.items():
            if metodo not in ("get", "post", "patch", "put", "delete"):
                continue
            clave = f"{metodo.upper()} {ruta}"
            info = {"params": sorted(p["name"] for p in op.get("parameters", []))}
            body = (op.get("requestBody", {}).get("content", {})
                      .get("application/json", {}).get("schema", {}).get("$ref"))
            if body:
                info["request"] = _campos(spec, body)
            resp = (op.get("responses", {}).get("200")
                    or op.get("responses", {}).get("201") or {})
            ref = resp.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
            if ref:
                info["response"] = _campos(spec, ref)
            out[clave] = info
    return out


def comparar(viejo: dict, nuevo: dict) -> list[str]:
    """Solo lo que puede romper: campos y endpoints que DESAPARECEN o cambian.

    Un campo NUEVO no rompe nada — se ignora, para que agregar features del lado
    del proveedor no obligue a tocar el snapshot.
    """
    problemas = []
    for clave, antes in viejo.items():
        ahora = nuevo.get(clave)
        if ahora is None:
            problemas.append(f"DESAPARECIO el endpoint: {clave}")
            continue
        if ahora.get("__falta__"):
            problemas.append(f"El motor ya no expone: {clave}")
            continue
        for seccion in ("params", "request", "response"):
            faltan = set(antes.get(seccion, [])) - set(ahora.get(seccion, []))
            if faltan:
                problemas.append(f"{clave} · {seccion}: desaparecieron {sorted(faltan)}")
    return problemas


async def main(escribir: bool) -> int:
    url = (settings.MAM_API_BASE_URL or "").rstrip("/")
    if not url:
        print("ERROR: falta MAM_API_BASE_URL")
        return 1

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{url}/openapi.json")
        r.raise_for_status()
        spec = r.json()

    actual = superficie(spec)
    version = spec.get("info", {}).get("version")

    if escribir or not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump({"version": version, "surface": actual}, f,
                      indent=2, ensure_ascii=False, sort_keys=True)
        print(f"Snapshot guardado ({len(actual)} operaciones, API {version}) -> {SNAPSHOT}")
        return 0

    with open(SNAPSHOT, encoding="utf-8") as f:
        guardado = json.load(f)

    problemas = comparar(guardado.get("surface", {}), actual)
    if guardado.get("version") != version:
        print(f"AVISO: la version del motor cambio: {guardado.get('version')} -> {version}")

    if problemas:
        print(f"\nEl contrato cambio en {len(problemas)} punto(s):\n")
        for p in problemas:
            print("  -", p)
        print("\nRevisar el impacto y, si esta contemplado, actualizar el snapshot con --write")
        return 1

    print(f"El contrato no cambio en lo que consumimos ({len(actual)} operaciones, API {version}).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--write" in sys.argv)))
