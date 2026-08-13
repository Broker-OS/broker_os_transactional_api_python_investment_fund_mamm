"""
Genera una clave Fernet nueva y la carga como MT5_CREDENTIALS_ENCRYPTION_KEY en el
.env local y en el del server (reusa scripts/set_env.py). NO imprime el valor.

⚠️ Ejecutar SOLO si todavía no hay cuentas MT5 creadas: cambiar la clave deja
ilegibles las credenciales ya cifradas. Respaldá la clave desde el .env del server.

Uso:  ./venv/bin/python scripts/set_mt5_key.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.fernet import Fernet  # noqa: E402

import set_env  # noqa: E402

if __name__ == "__main__":
    key = Fernet.generate_key().decode()
    print("Clave Fernet generada (44 chars). Se carga en .env local + server sin mostrarla.")
    raise SystemExit(set_env.main("MT5_CREDENTIALS_ENCRYPTION_KEY", key))
