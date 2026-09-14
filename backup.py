"""Backup automático do banco.

Grava uma cópia datada em backups/reforma-AAAA-MM-DD.db usando a API de backup
do SQLite (cópia consistente mesmo com o app aberto), commita na branch main,
envia a main para o GitHub em segundo plano e, se houver OneDrive, copia também
para lá.

É chamado quando o app abre e depois de cada alteração. Se nada mudou desde o
último backup, não faz nada. Várias alterações no mesmo dia sobrescrevem a cópia
do dia, mas cada versão fica guardada no histórico do git.

Commit: o backup é commitado na branch atual (para a pasta de trabalho ficar
limpa) e, se ela não for a main, o mesmo arquivo também é commitado direto na
main, sem trocar de branch. Só a main é enviada.

Push: roda em segundo plano e nunca pede senha (usa a credencial já salva). Se
falhar (sem internet, main do GitHub com commits que não estão aqui), só avisa
no terminal; o próximo backup, ou a próxima abertura do app, tenta de novo e
envia tudo o que ficou pendente.

Variáveis de ambiente:
    REFORMA_BACKUP_DIR     pasta dos backups (padrão: backups/ ao lado do app)
    REFORMA_BACKUP_NUVEM   pasta extra para copiar (padrão: %OneDrive%/Backups/spend-control);
                           use 0 para desativar
    REFORMA_BACKUP_BRANCH  branch que recebe os backups e é enviada (padrão: main)
    REFORMA_BACKUP_REMOTE  remoto do push (padrão: origin)
    REFORMA_BACKUP_PUSH    use 0 para desativar o push automático
"""

import glob
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from datetime import date

import db

BACKUP_DIR = os.environ.get("REFORMA_BACKUP_DIR", os.path.join(db.BASE_DIR, "backups"))
BRANCH = os.environ.get("REFORMA_BACKUP_BRANCH", "main")
REMOTE = os.environ.get("REFORMA_BACKUP_REMOTE", "origin")

_lock = threading.Lock()
_push_lock = threading.Lock()
_push_pendente = threading.Event()


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


def _git(*args, env=None, timeout=30):
    return subprocess.run(
        ["git", "-C", BACKUP_DIR, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        env={**os.environ, **env} if env else None,
    )


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------

def _commit(caminho):
    mensagem = f"Backup do banco: {os.path.basename(caminho)}"
    try:
        atual = _git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        if atual:
            add = _git("add", "--", caminho)
            if add.returncode != 0:
                print(f"[backup] git add falhou: {add.stderr.strip()}")
                return
            commit = _git("commit", "--quiet", "-m", mensagem, "--", caminho)
            if commit.returncode != 0 and "nothing" not in (commit.stdout + commit.stderr):
                print(f"[backup] git commit falhou: {(commit.stderr or commit.stdout).strip()}")
        if atual != BRANCH:
            _commit_direto_na_branch(caminho, mensagem)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[backup] git indisponível: {e}")


def _commit_direto_na_branch(caminho, mensagem):
    """Commita `caminho` em BRANCH sem trocar de branch nem mexer na pasta de trabalho."""
    antigo = _git("rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}^{{commit}}").stdout.strip()
    if not antigo:
        print(f"[backup] a branch {BRANCH} não existe; backup não commitado nela")
        return
    rel = _git("rev-parse", "--show-prefix").stdout.strip() + os.path.basename(caminho)
    blob = _git("hash-object", "-w", "--", caminho)
    if blob.returncode != 0:
        print(f"[backup] git hash-object falhou: {blob.stderr.strip()}")
        return

    # índice temporário com a árvore da BRANCH + o arquivo novo
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        for passo in (("read-tree", antigo),
                      ("update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.strip()},{rel}")):
            r = _git(*passo, env=env)
            if r.returncode != 0:
                print(f"[backup] git {passo[0]} falhou: {r.stderr.strip()}")
                return
        arvore = _git("write-tree", env=env)
    if arvore.returncode != 0:
        print(f"[backup] git write-tree falhou: {arvore.stderr.strip()}")
        return
    arvore = arvore.stdout.strip()
    if arvore == _git("rev-parse", f"{antigo}^{{tree}}").stdout.strip():
        return  # a BRANCH já tem exatamente este backup

    novo = _git("commit-tree", arvore, "-p", antigo, "-m", mensagem)
    if novo.returncode != 0:
        print(f"[backup] git commit-tree falhou: {novo.stderr.strip()}")
        return
    # o valor antigo garante que ninguém mexeu na BRANCH entre a leitura e a gravação
    ref = _git("update-ref", "-m", mensagem, f"refs/heads/{BRANCH}", novo.stdout.strip(), antigo)
    if ref.returncode != 0:
        print(f"[backup] não consegui atualizar a {BRANCH}: {ref.stderr.strip()}")


# --------------------------------------------------------------------------
# Push
# --------------------------------------------------------------------------

def _push():
    try:
        pendentes = _git("rev-list", "--count", f"refs/remotes/{REMOTE}/{BRANCH}..refs/heads/{BRANCH}")
        if pendentes.returncode == 0 and pendentes.stdout.strip() == "0":
            return True  # nada novo para enviar
        r = _git(
            "push", "--quiet", REMOTE, f"refs/heads/{BRANCH}:refs/heads/{BRANCH}",
            env={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[backup] push não rodou (backups seguem salvos localmente): {e}")
        return False
    if r.returncode == 0:
        print(f"[backup] {BRANCH} enviada para {REMOTE}")
        return True
    saida = (r.stderr + r.stdout).strip()
    if "rejected" in saida or "fetch first" in saida or "non-fast-forward" in saida:
        print(f"[backup] push recusado: a {BRANCH} do {REMOTE} tem commits que não estão aqui. "
              f"Rode 'git pull' na {BRANCH}; os backups seguem salvos localmente.")
    else:
        print(f"[backup] push falhou (backups seguem salvos localmente e serão enviados depois): {saida}")
    return False


def enviar_para_github(em_segundo_plano=True):
    """Envia a BRANCH para o REMOTE.

    Em segundo plano, um pedido que chega com um push em andamento é atendido
    por esse mesmo push assim que ele termina. Retorna o resultado só no modo
    síncrono (None se o push estiver desativado).
    """
    if os.environ.get("REFORMA_BACKUP_PUSH") == "0":
        return None
    if not em_segundo_plano:
        with _push_lock:
            return _push()
    _push_pendente.set()
    if _push_lock.acquire(blocking=False):
        threading.Thread(target=_trabalhar_push, daemon=True).start()
    return None


def _trabalhar_push():
    try:
        while _push_pendente.is_set():
            _push_pendente.clear()
            _push()
    finally:
        _push_lock.release()
    if _push_pendente.is_set():  # pedido chegou entre o fim do laço e a liberação
        enviar_para_github()


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------

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
        enviar_para_github()

        nuvem = _pasta_nuvem()
        if nuvem:
            try:
                os.makedirs(nuvem, exist_ok=True)
                shutil.copy2(destino, os.path.join(nuvem, os.path.basename(destino)))
            except OSError as e:
                print(f"[backup] não consegui copiar para {nuvem}: {e}")
        return destino
