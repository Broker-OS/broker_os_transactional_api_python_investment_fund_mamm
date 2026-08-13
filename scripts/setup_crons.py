"""Deja armados los procesos periodicos como timers de systemd.

    python -m scripts.setup_crons        # instala y arranca
    python -m scripts.setup_crons --dry  # muestra las unidades sin instalar

Tres cosas del dominio no son instantaneas y sin esto nadie las empuja:

  * una baja con cierre de posiciones queda en STOPPING hasta que cierran;
  * los fees se acreditan segun el periodo del leader, despues del corte;
  * un aviso de terminacion que llega antes que su suscripcion queda pendiente.

Cada job es un `curl` contra un endpoint que ya existe y ya es idempotente, asi
que correrlos de mas no rompe nada — la unica consecuencia de un intervalo corto
es mas trafico.

Se usa una CUENTA DE SERVICIO propia, con rol ADMIN pero SIN notificaciones: los
jobs necesitan permisos para operar, pero su email es un buzon que no existe y
cada aviso rebotaria.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SERVICIO = "broker-os-mam"
BASE = "http://127.0.0.1:8200/api/v1"
ENV_FILE = "/etc/broker-os-mam-cron.env"

# (nombre, descripcion, endpoint, cada cuanto, timeout)
JOBS = [
    ("allocations-sync",
     "Sincronizar suscripciones que estan cerrando posiciones",
     "/mam/allocations/sync", "10min", 60),
    ("deletions-sync",
     "Seguir las bajas de cuenta en curso",
     "/mam/account-deletions/sync", "10min", 60),
    ("webhook-retry",
     "Reintentar avisos de terminacion sin aplicar",
     "/mam/webhook-events/retry", "15min", 60),
    ("perf-fee-reconcile",
     "Conciliar los fees cobrados por todas las estrategias",
     "/mam/perf-fee/reconcile", "2h", 300),
]


def unidad_service(nombre, descripcion, endpoint, timeout):
    # --fail hace que un 5xx marque el job como fallido en systemd en vez de
    # pasar por exitoso; sin eso, un endpoint roto se ve "activo" para siempre.
    metodo = "-X POST"
    datos = ' -H "Content-Type: application/json" -d "{}"' if endpoint.endswith("reconcile") else ""
    return f"""[Unit]
Description={SERVICIO} — {descripcion}
After=network.target {SERVICIO}.service

[Service]
Type=oneshot
EnvironmentFile={ENV_FILE}
ExecStart=/usr/bin/curl -fsS --max-time {timeout} {metodo} \\
  -H "X-API-Key: ${{CRON_API_KEY}}"{datos} \\
  {BASE}{endpoint}
"""


def unidad_timer(nombre, descripcion, cada):
    return f"""[Unit]
Description={SERVICIO} — {descripcion} (cada {cada})

[Timer]
OnBootSec=5min
OnUnitActiveSec={cada}
# Evita que todos los jobs salgan en el mismo segundo tras un reinicio.
RandomizedDelaySec=45s
Persistent=true

[Install]
WantedBy=timers.target
"""


def main(dry: bool) -> int:
    print(f"Servicio: {SERVICIO}\nBase: {BASE}\nEnvFile: {ENV_FILE}\n")
    for nombre, desc, endpoint, cada, timeout in JOBS:
        print(f"── {SERVICIO}-{nombre} ({cada}) → {endpoint}")
        if dry:
            print(unidad_service(nombre, desc, endpoint, timeout))
            print(unidad_timer(nombre, desc, cada))
    if dry:
        print("(--dry: no se instalo nada)")
        return 0

    print("\nEste script genera el contenido; la instalacion la hace el deploy con sudo.")
    print("Ver README, seccion 'Trabajo periodico'.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry" in sys.argv))
