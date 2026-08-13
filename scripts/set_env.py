"""
Upsert de una variable de entorno en el .env local + el .env del server (por SFTP)
y reinicio del servicio. Uso puntual, sin exponer los secretos del .env.

Uso:  python scripts/set_env.py APP_NAME "Social Trading Bridge Markets"
"""
from __future__ import annotations

import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
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


def upsert(lines: list[str], key: str, value: str) -> list[str]:
    out: list[str] = []
    seen = False
    for line in lines:
        s = line.strip()
        k = s.split("=", 1)[0].strip() if ("=" in s and not s.startswith("#")) else None
        if k == key:
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}")
    return out


def main(key: str, value: str) -> int:
    # local
    local_lines = BRIDGE_ENV.read_text(encoding="utf-8").splitlines() if BRIDGE_ENV.exists() else []
    BRIDGE_ENV.write_text("\n".join(upsert(local_lines, key, value)) + "\n", encoding="utf-8")
    print(f"local .env -> {key} actualizado")

    cfg = parse_env(BRIDGE_ENV.read_text(encoding="utf-8"))
    host, user, pwd = cfg.get("DEPLOY_SSH_HOST"), cfg.get("DEPLOY_SSH_USER"), cfg.get("DEPLOY_SSH_PASSWORD")
    rdir, svc = cfg.get("DEPLOY_REMOTE_DIR"), cfg.get("DEPLOY_SERVICE_NAME", "Bridge Markets-bridge")
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
        with sftp.open(remote_env, "w") as f:
            f.write(("\n".join(upsert(remote_lines, key, value)) + "\n").encode("utf-8"))
        sftp.close()
        print(f"server .env -> {key} actualizado")

        cmd = f"sudo -S -p '' systemctl restart {svc} && sleep 2 && systemctl is-active {svc}"
        stdin, stdout, stderr = ssh.exec_command(cmd)
        stdin.write(pwd + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        print("restart is-active:", stdout.read().decode().strip() or "(sin salida)")
    finally:
        ssh.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python scripts/set_env.py KEY VALUE")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
