"""
Verifica el filtro de redaccion de secretos. Corre sin base ni red.

Esta suite existe por un bug que estuvo diez commits sin que nadie lo notara:
`redact()` referenciaba un setting que no existe en este servicio (sobraba del
bridge PAMM), levantaba AttributeError con CADA registro, y el `except` del
filtro reemplazaba el mensaje por "[log redaction failed]". El servicio andaba
perfecto y todos los logs decian lo mismo.

De ahi las dos mitades de esta suite, que son igual de importantes:

  * que los secretos NO se filtren (para lo que se escribio el filtro);
  * que los mensajes SI se lean (porque un filtro que suprime todo tambien
    "cumple" la primera mitad, y deja el sistema ciego).
"""
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import settings  # noqa: E402
from app.core.log_redaction import MASK, SecretRedactionFilter, redact  # noqa: E402

fails = []


def check(label, ok, extra=""):
    print(("  [OK] " if ok else "  [FALLO] ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(label)


API_KEY = "mam-key-JQ8fT2wLp0xZv5nRq7Ke"
WEBHOOK_SECRET = "whsec-KEtHnuOWtgAN4dTHLGRP5rILSqTD09aS"
FERNET_KEY = "dGVzdC1mZXJuZXQta2V5LXBhcmEtbGEtc3VpdGUtMDE9"

settings.MAM_API_KEY = API_KEY
settings.MAM_WEBHOOK_SIGNING_SECRET = WEBHOOK_SECRET
settings.MT5_CREDENTIALS_ENCRYPTION_KEY = FERNET_KEY


def sin_fuga(texto: str, *secretos: str) -> bool:
    return all(s not in texto for s in secretos)


print("\n1. Secretos con su clave delante (JSON, dict, query string)")
casos = [
    ('{"api_key": "%s"}' % API_KEY, API_KEY),
    ("{'password': 'Tr4d1ng!2026'}", "Tr4d1ng!2026"),
    ('{"mt5_password": "Abc123!x", "mt5_investor_password": "Inv456!y"}', "Abc123!x"),
    ("X-API-Key=%s&login=500123" % API_KEY, API_KEY),
    ('{"authorization": "%s"}' % API_KEY, API_KEY),
]
for crudo, secreto in casos:
    salida = redact(crudo)
    check(f"enmascara {crudo[:38]}...", sin_fuga(salida, secreto) and MASK in salida, salida)

print("\n2. Bearer token")
salida = redact(f"Authorization: Bearer {API_KEY}")
check("enmascara el Bearer", sin_fuga(salida, API_KEY), salida)

print("\n3. Secretos SUELTOS, sin ninguna clave que los preceda")
# Es como aparecen de verdad: en un traceback de httpx, en el repr de un request.
for nombre, secreto in (("MAM_API_KEY", API_KEY),
                        ("MAM_WEBHOOK_SIGNING_SECRET", WEBHOOK_SECRET),
                        ("MT5_CREDENTIALS_ENCRYPTION_KEY", FERNET_KEY)):
    salida = redact(f"httpx.ConnectError al llamar con {secreto} en el header")
    check(f"barre {nombre} por valor literal", sin_fuga(salida, secreto), salida)

print("\n4. El texto que NO es secreto se conserva legible")
# La otra mitad: un filtro que rompe todo tambien "no filtra secretos".
mensaje = "Movimiento 4f2a completado: deposito de 1500.00 USD en la cuenta 500123"
salida = redact(mensaje)
check("un mensaje sin secretos pasa intacto", salida == mensaje, salida)

print("\n5. El filtro no rompe ningun LogRecord (la regresion del bug)")
f = SecretRedactionFilter()
SecretRedactionFilter._fallo_reportado = False


def pasar(msg, args=None):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    f.filter(rec)
    return rec.getMessage()


check("mensaje simple sobrevive", pasar("Config check: sin observaciones.")
      == "Config check: sin observaciones.")
check("mensaje con args %s sobrevive",
      pasar("Movimiento %s: %s", ("4f2a", "OK")) == "Movimiento 4f2a: OK")
# El dict va envuelto en una tupla, que es como llega desde `logger.info(msg, d)`:
# LogRecord lo desenvuelve solo, y recien ahi `record.args` es el dict.
check("args en dict sobreviven",
      pasar("cuenta %(login)s", ({"login": "500123"},)) == "cuenta 500123")
check("args no-str (int, None) no rompen",
      pasar("reintento %s de %s", (3, None)) == "reintento 3 de None")
salida = pasar("llamando con %s", (API_KEY,))
check("un secreto en los args se enmascara", sin_fuga(salida, API_KEY), salida)

# El template ya trae `api_key=` y el valor llega por argumento. Si se redacta
# el template antes de interpolar, la regex se come el `%s` y el registro
# revienta al emitirse. Es la forma normal de loguear en Python.
salida = pasar("llamando con api_key=%s", (API_KEY,))
check("un placeholder detras de una clave secreta no rompe el registro",
      sin_fuga(salida, API_KEY) and MASK in salida, salida)
salida = pasar("password=%s para la cuenta %s", ("Tr4d1ng!2026", "500123"))
check("varios placeholders con clave secreta delante",
      sin_fuga(salida, "Tr4d1ng!2026") and "500123" in salida, salida)

check("NINGUN registro salio suprimido", not SecretRedactionFilter._fallo_reportado)

print("\n6. Casos borde que no deben levantar")
for label, valor in (("texto vacio", ""), ("None", None)):
    try:
        redact(valor)
        check(f"redact({label}) no levanta", True)
    except Exception as e:  # noqa: BLE001
        check(f"redact({label}) no levanta", False, type(e).__name__)

# Un secreto vacio no puede convertir cada string en ***REDACTED***.
settings.MAM_API_KEY = ""
salida = redact("mensaje comun y corriente")
check("un secreto vacio no enmascara todo", salida == "mensaje comun y corriente", salida)
settings.MAM_API_KEY = API_KEY

print("\n" + "=" * 60)
print("FALLOS:", len(fails))
for x in fails:
    print("  -", x)
sys.exit(1 if fails else 0)
