"""
Copia las credenciales AWS SES desde el .env de BrokerOS al .env del bridge
(local + server) SIN imprimir los valores. Uso puntual.

- Lee AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION / AWS_SES_SENDER_EMAIL
  del .env de BrokerOS (proyecto hermano).
- Hace upsert en el .env local del bridge.
- Hace upsert en el .env del server (por SFTP, usando los DEPLOY_SSH_* del .env local).
- Reinicia el servicio.

Solo imprime NOMBRES de variables y confirmaciones, nunca valores.

Uso:  python scripts/sync_email_env.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import paramiko

AWS_KEYS = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_SES_SENDER_EMAIL"]

ROOT = Path(__file__).resolve().parent.parent
BROKEROS_ENV = ROOT.parent / "broker_os_transactional_api_python" / ".env"
BRIDGE_ENV = ROOT / ".env"


def parse_env(text: str) -> dict[str, str]:
    d: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        d[k.strip()] = v.strip()
    return d


def upsert_lines(existing: list[str], values: dict[str, str], header: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in existing:
        s = line.strip()
        key = s.split("=", 1)[0].strip() if ("=" in s and not s.startswith("#")) else None
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    added = [k for k in values if k not in seen]
    if added:
        out.append("")
        out.append(header)
        for k in added:
            out.append(f"{k}={values[k]}")
    return out


def main() -> int:
    if not BROKEROS_ENV.exists():
        print(f"ERROR: no existe el .env de BrokerOS en {BROKEROS_ENV}")
        return 1
    src = parse_env(BROKEROS_ENV.read_text(encoding="utf-8"))
    values = {k: src[k] for k in AWS_KEYS if src.get(k)}
    missing = [k for k in AWS_KEYS if k not in values]
    print("AWS vars encontradas en BrokerOS:", sorted(values.keys()))
    if missing:
        print("FALTAN en BrokerOS .env:", missing)
    if not values:
        print("Nada para copiar.")
        return 1

    # ── local ──
    local_lines = BRIDGE_ENV.read_text(encoding="utf-8").splitlines() if BRIDGE_ENV.exists() else []
    new_local = upsert_lines(local_lines, values, "# AWS SES (email OTP) — sincronizado desde BrokerOS")
    BRIDGE_ENV.write_text("\n".join(new_local) + "\n", encoding="utf-8")
    print("local .env actualizado.")

    # ── server (SFTP) ──
    cfg = parse_env(BRIDGE_ENV.read_text(encoding="utf-8"))
    host = cfg.get("DEPLOY_SSH_HOST")
    user = cfg.get("DEPLOY_SSH_USER")
    pwd = cfg.get("DEPLOY_SSH_PASSWORD")
    rdir = cfg.get("DEPLOY_REMOTE_DIR")
    svc = cfg.get("DEPLOY_SERVICE_NAME", "Bridge Markets-bridge")
    if not all([host, user, pwd, rdir]):
        print("Faltan DEPLOY_SSH_* en el .env local; no puedo actualizar el server.")
        return 1

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=pwd, timeout=30)
    try:
        sftp = ssh.open_sftp()
        remote_env = f"{rdir}/.env"
        try:
            with sftp.open(remote_env, "r") as f:
                remote_lines = f.read().decode("utf-8").splitlines()
        except IOError:
            remote_lines = []
        new_remote = upsert_lines(remote_lines, values, "# AWS SES (email OTP)")
        with sftp.open(remote_env, "w") as f:
            f.write(("\n".join(new_remote) + "\n").encode("utf-8"))
        sftp.close()
        print("server .env actualizado.")

        cmd = f"sudo -S -p '' systemctl restart {svc} && sleep 2 && systemctl is-active {svc}"
        stdin, stdout, stderr = ssh.exec_command(cmd)
        stdin.write(pwd + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        print("restart is-active:", out or "(sin salida)")
        if err:
            print("stderr:", err[:200])
    finally:
        ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
