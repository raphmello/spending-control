"""Backup automático do banco.

Grava uma cópia datada em backups/reforma-AAAA-MM-DD.db usando a API de backup
do SQLite (cópia consistente mesmo com o app aberto), faz commit dela no git e,
se houver OneDrive, copia também para lá.

É chamado quando o app abre e depois de cada alteração. Se nada mudou desde o
último backup, não faz nada. Várias alterações no mesmo dia sobrescrevem a cópia
do dia, mas cada versão fica guardada no histórico do git.

Variáveis de ambiente:
    REFORMA_BACKUP_DIR    pasta dos backups (padrão: backups/ ao lado do app)
    REFORMA_BACKUP_NUVEM  pasta extra para copiar (padrão: %OneDrive%/Backups/spend-control);
                          use 0 para desativar
"""

import glob
import hashlib
import os
import shutil
import sqlite3
import subprocess
import threading
from datetime import date

import db

BACKUP_DIR = os.environ.get("REFORMA_BACKUP_DIR", os.path.join(db.BASE_DIR, "backups"))

_lock = threading.Lock()


def _pasta_nuvem():
    destino = os.environ.get("REFORMA_BACKUP_NUVEM")
    if destino == "0":
        return None
    if destino:
        return destino
    onedrive = os.environ.get("OneDrive")
    if onedrive and os.path.isdir(onedrive):
        return os.path.join(onedrive, "Backups", "spend-control")
    return None


def _hash(caminho):
    with open(caminho, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _ultimo_backup():
    arquivos = sorted(glob.glob(os.path.join(BACKUP_DIR, "reforma-*.db")))
    return arquivos[-1] if arquivos else None


def _git(*args):
    return subprocess.run(
        ["git", "-C", BACKUP_DIR, *args],
        capture_output=True, text=True, timeout=30,
    )


def _commit(caminho):
    nome = os.path.basename(caminho)
    try:
        add = _git("add", "--", caminho)
        if add.returncode != 0:
            print(f"[backup] git add falhou: {add.stderr.strip()}")
            return
        commit = _git("commit", "--quiet", "-m", f"Backup do banco: {nome}", "--", caminho)
        if commit.returncode != 0 and "nothing" not in (commit.stdout + commit.stderr):
            print(f"[backup] git commit falhou: {(commit.stderr or commit.stdout).strip()}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[backup] git indisponível: {e}")


def fazer_backup():
    """Cria/atualiza o backup do dia. Retorna o caminho do backup ou None se nada mudou."""
    if not os.path.exists(db.DB_PATH):
        return None
    with _lock:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        destino = os.path.join(BACKUP_DIR, f"reforma-{date.today().isoformat()}.db")
        temporario = destino + ".tmp"

        origem = sqlite3.connect(db.DB_PATH)
        copia = sqlite3.connect(temporario)
        try:
            origem.backup(copia)
        finally:
            copia.close()
            origem.close()

        ultimo = _ultimo_backup()
        if ultimo and _hash(ultimo) == _hash(temporario):
            os.remove(temporario)
            return None

        os.replace(temporario, destino)
        print(f"[backup] {destino}")
        _commit(destino)

        nuvem = _pasta_nuvem()
        if nuvem:
            try:
                os.makedirs(nuvem, exist_ok=True)
                shutil.copy2(destino, os.path.join(nuvem, os.path.basename(destino)))
            except OSError as e:
                print(f"[backup] não consegui copiar para {nuvem}: {e}")
        return destino
