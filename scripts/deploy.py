"""
Deploy del servicio al server por SSH. Lee los accesos del `.env` del proyecto:

    DEPLOY_SSH_HOST, DEPLOY_SSH_USER, DEPLOY_SSH_PASSWORD
    DEPLOY_REMOTE_DIR   (opcional; default /home/<user>/broker-os-mam)
    DEPLOY_SERVICE_NAME (opcional; default broker-os-mam)
    DEPLOY_HEALTH_PORT  (opcional; default 8200)

Qué hace:
  1. Empaqueta el proyecto (excluye venv/.env/__pycache__/.git).
  2. Lo sube al server y reemplaza `app/` (borra el viejo para no dejar archivos huérfanos).
  3. `pip install -r requirements.txt` + `alembic upgrade head`.
  4. `sudo systemctl restart <servicio>` y verifica el health local.

El `.env` y el `venv` del server NO se tocan (se preservan). No imprime credenciales.

Uso:   ./venv/bin/python scripts/deploy.py      (o  python scripts/deploy.py)
Requiere paramiko (está en requirements.txt).
"""
import io
import os
import sys
import tarfile
import time

_EXCLUDE_DIRS = {"venv", "__pycache__", ".git", ".pytest_cache", ".history"}


def _load_env(path: str) -> dict:
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[k.strip()] = v
    return vals


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = _load_env(os.path.join(ROOT, ".env"))
host = env.get("DEPLOY_SSH_HOST")
user = env.get("DEPLOY_SSH_USER")
pwd = env.get("DEPLOY_SSH_PASSWORD")
rdir = env.get("DEPLOY_REMOTE_DIR") or (f"/home/{user}/broker-os-mam" if user else "")
svc = env.get("DEPLOY_SERVICE_NAME") or "broker-os-mam"
port = env.get("DEPLOY_HEALTH_PORT") or "8200"

if not (host and user and pwd):
    print("ERROR: faltan DEPLOY_SSH_HOST / DEPLOY_SSH_USER / DEPLOY_SSH_PASSWORD en el .env")
    sys.exit(1)

try:
    import paramiko
except ImportError:
    print("ERROR: falta paramiko. Instalar:  ./venv/bin/pip install -r requirements.txt")
    sys.exit(1)


def _filter(ti: "tarfile.TarInfo"):
    parts = ti.name.split("/")
    if any(p in _EXCLUDE_DIRS for p in parts):
        return None
    base = parts[-1]
    if base == ".env" or base.endswith(".pyc"):
        return None
    return ti


buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as t:
    t.add(ROOT, arcname=".", filter=_filter)
buf.seek(0)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(hostname=host, username=user, password=pwd, timeout=30,
            look_for_keys=False, allow_agent=False)


def run(cmd: str, label: str, sudo: bool = False):
    full = f"sudo -S -p '' {cmd}" if sudo else cmd
    _in, _out, _err = ssh.exec_command(full, timeout=240)
    if sudo:
        _in.write(pwd + "\n"); _in.flush(); _in.channel.shutdown_write()
    out = _out.read().decode("utf-8", "replace")
    err = _err.read().decode("utf-8", "replace")
    rc = _out.channel.recv_exit_status()
    print(f"--- {label} (rc={rc}) ---")
    for ln in out.strip().splitlines()[-8:]:
        print("  ", ln)
    if rc != 0 and err.strip():
        for ln in err.strip().splitlines()[-4:]:
            if "password" not in ln.lower():
                print("   ERR:", ln)
    return rc, out


home = f"/home/{user}"
sftp = ssh.open_sftp()
sftp.putfo(buf, f"{home}/mam_deploy.tar.gz")
sftp.close()
print("codigo subido")

def _abortar(motivo: str):
    print(f"FALLO: {motivo}")
    ssh.close()
    sys.exit(1)


# La migracion NO se encadena con `| tail`: el exit status de un pipe es el del
# ULTIMO comando, asi que un `alembic upgrade` fallido pasaria por exitoso y el
# servicio arrancaria contra el esquema viejo. Se redirige a un log y se mira el
# codigo de salida de alembic directamente (`pipefail` no existe en dash).
rc, out = run(
    f"set -e; mkdir -p {rdir}; rm -rf {rdir}/app; "
    f"tar xzf {home}/mam_deploy.tar.gz -C {rdir}; rm -f {home}/mam_deploy.tar.gz; "
    f"cd {rdir}; (test -d venv || python3.12 -m venv venv || python3 -m venv venv); "
    f"./venv/bin/pip install -q -r requirements.txt; "
    f"if ./venv/bin/alembic upgrade head > /tmp/mam_migrate.log 2>&1; then "
    f"  tail -2 /tmp/mam_migrate.log; "
    f"else "
    f"  echo MIGRATE_FAIL; tail -20 /tmp/mam_migrate.log; exit 1; "
    f"fi; echo SYNC_OK",
    "sync + deps + migrate",
)
if "MIGRATE_FAIL" in out:
    _abortar("la migracion no aplico. El codigo NO se activa: el servicio sigue "
             "con la version anterior. Revisar /tmp/mam_migrate.log en el server.")
if rc != 0 or "SYNC_OK" not in out:
    _abortar("sync/deps/migrate no termino bien")

rc, _ = run(f"systemctl restart {svc}", "restart servicio", sudo=True)
if rc != 0:
    run(f"journalctl -u {svc} -n 25 --no-pager", "journal (fallo)", sudo=True)
    _abortar("systemctl restart devolvio error")

time.sleep(3)
rc, out = run(f"systemctl is-active {svc}", "is-active", sudo=True)
if "active" not in out:
    run(f"journalctl -u {svc} -n 25 --no-pager", "journal (fallo)", sudo=True)
    _abortar(f"el servicio {svc} no quedo activo")

# /health ahora consulta la base y resume los chequeos de configuracion. Que el
# proceso responda no alcanza: con Postgres caido o un secreto faltante el
# deploy no puede darse por bueno.
rc, out = run(f"curl -s --max-time 8 http://127.0.0.1:{port}/health || echo '(sin respuesta)'",
              "health local")
if '"status": "healthy"' not in out and '"status":"healthy"' not in out:
    run(f"journalctl -u {svc} -n 25 --no-pager", "journal (fallo)", sudo=True)
    _abortar("/health no responde 'healthy' (mirar 'database' y 'config' en la respuesta)")
if '"critical": 0' not in out and '"critical":0' not in out:
    print("AVISO: /health reporta observaciones CRITICAL de configuracion.")
    print("       El servicio esta arriba pero hay funciones que van a fallar al usarse.")
    print("       Ver GET /api/v1/mam/ops/readiness para el detalle.")

ssh.close()
print("DEPLOY LISTO")
