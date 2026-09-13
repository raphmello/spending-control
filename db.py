"""Camada de acesso ao banco (SQLite).

Todos os valores monetarios sao armazenados em CENTAVOS (INTEGER),
para evitar erros de arredondamento de ponto flutuante.
"""

import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("REFORMA_DB", os.path.join(BASE_DIR, "reforma.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pessoa (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    nome    TEXT NOT NULL UNIQUE,
    ativo   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS despesa (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nome          TEXT NOT NULL,
    categoria     TEXT,
    observacao    TEXT,
    valor_total   INTEGER NOT NULL,          -- centavos
    qtd_parcelas  INTEGER NOT NULL,
    criado_em     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS parcela (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    despesa_id  INTEGER NOT NULL REFERENCES despesa(id) ON DELETE CASCADE,
    numero      INTEGER NOT NULL,
    valor       INTEGER NOT NULL,            -- centavos
    vencimento  TEXT,                        -- YYYY-MM-DD (opcional)
    UNIQUE (despesa_id, numero)
);

CREATE TABLE IF NOT EXISTS pagamento (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parcela_id  INTEGER NOT NULL REFERENCES parcela(id) ON DELETE CASCADE,
    pessoa_id   INTEGER NOT NULL REFERENCES pessoa(id),
    valor       INTEGER NOT NULL,            -- centavos
    data        TEXT,                        -- YYYY-MM-DD (opcional)
    observacao  TEXT,
    criado_em   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_parcela_despesa   ON parcela(despesa_id);
CREATE INDEX IF NOT EXISTS idx_pagamento_parcela ON pagamento(parcela_id);
CREATE INDEX IF NOT EXISTS idx_pagamento_pessoa  ON pagamento(pessoa_id);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Pessoas
# --------------------------------------------------------------------------

def listar_pessoas(somente_ativas=False):
    with get_db() as conn:
        sql = "SELECT * FROM pessoa"
        if somente_ativas:
            sql += " WHERE ativo = 1"
        sql += " ORDER BY nome COLLATE NOCASE"
        return conn.execute(sql).fetchall()


def criar_pessoa(nome):
    with get_db() as conn:
        conn.execute("INSERT INTO pessoa (nome) VALUES (?)", (nome.strip(),))


def alternar_pessoa(pessoa_id):
    with get_db() as conn:
        conn.execute("UPDATE pessoa SET ativo = 1 - ativo WHERE id = ?", (pessoa_id,))


def excluir_pessoa(pessoa_id):
    """Só exclui se a pessoa não tiver pagamentos lançados."""
    with get_db() as conn:
        usada = conn.execute(
            "SELECT COUNT(*) FROM pagamento WHERE pessoa_id = ?", (pessoa_id,)
        ).fetchone()[0]
        if usada:
            return False
        conn.execute("DELETE FROM pessoa WHERE id = ?", (pessoa_id,))
        return True


# --------------------------------------------------------------------------
# Despesas / parcelas
# --------------------------------------------------------------------------

def dividir_parcelas(valor_total, qtd):
    """Divide o total em `qtd` parcelas de centavos; sobra vai na última."""
    base = valor_total // qtd
    valores = [base] * qtd
    valores[-1] += valor_total - base * qtd
    return valores


def criar_despesa(nome, valor_total, qtd_parcelas, categoria=None,
                  observacao=None, valores_parcelas=None, vencimentos=None):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO despesa (nome, categoria, observacao, valor_total, qtd_parcelas)
               VALUES (?, ?, ?, ?, ?)""",
            (nome.strip(), categoria, observacao, valor_total, qtd_parcelas),
        )
        despesa_id = cur.lastrowid
        valores = valores_parcelas or dividir_parcelas(valor_total, qtd_parcelas)
        for i, valor in enumerate(valores, start=1):
            venc = (vencimentos or {}).get(i)
            conn.execute(
                "INSERT INTO parcela (despesa_id, numero, valor, vencimento) VALUES (?, ?, ?, ?)",
                (despesa_id, i, valor, venc),
            )
        return despesa_id


def excluir_despesa(despesa_id):
    with get_db() as conn:
        conn.execute("DELETE FROM despesa WHERE id = ?", (despesa_id,))


def atualizar_parcela(parcela_id, valor, vencimento):
    """Atualiza o valor/vencimento de uma parcela e sincroniza o total da despesa."""
    with get_db() as conn:
        row = conn.execute("SELECT despesa_id FROM parcela WHERE id = ?", (parcela_id,)).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE parcela SET valor = ?, vencimento = ? WHERE id = ?",
            (valor, vencimento, parcela_id),
        )
        conn.execute(
            """UPDATE despesa
                  SET valor_total = (SELECT COALESCE(SUM(valor),0) FROM parcela WHERE despesa_id = ?)
                WHERE id = ?""",
            (row["despesa_id"], row["despesa_id"]),
        )


def obter_despesa(despesa_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM despesa WHERE id = ?", (despesa_id,)).fetchone()


def listar_despesas():
    """Despesas com total pago e saldo."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT d.*,
                   COALESCE((
                       SELECT SUM(pg.valor)
                         FROM pagamento pg
                         JOIN parcela pc ON pc.id = pg.parcela_id
                        WHERE pc.despesa_id = d.id
                   ), 0) AS pago
              FROM despesa d
             ORDER BY d.criado_em DESC, d.id DESC
            """
        ).fetchall()


def listar_parcelas(despesa_id):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT pc.*,
                   COALESCE((SELECT SUM(valor) FROM pagamento WHERE parcela_id = pc.id), 0) AS pago
              FROM parcela pc
             WHERE pc.despesa_id = ?
             ORDER BY pc.numero
            """,
            (despesa_id,),
        ).fetchall()


# --------------------------------------------------------------------------
# Pagamentos
# --------------------------------------------------------------------------

def criar_pagamento(parcela_id, pessoa_id, valor, data=None, observacao=None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO pagamento (parcela_id, pessoa_id, valor, data, observacao)
               VALUES (?, ?, ?, ?, ?)""",
            (parcela_id, pessoa_id, valor, data or None, observacao or None),
        )


def excluir_pagamento(pagamento_id):
    with get_db() as conn:
        row = conn.execute(
            """SELECT pc.despesa_id FROM pagamento pg
                 JOIN parcela pc ON pc.id = pg.parcela_id
                WHERE pg.id = ?""",
            (pagamento_id,),
        ).fetchone()
        conn.execute("DELETE FROM pagamento WHERE id = ?", (pagamento_id,))
        return row["despesa_id"] if row else None


def listar_pagamentos_despesa(despesa_id):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT pg.*, ps.nome AS pessoa, pc.numero AS parcela_numero
              FROM pagamento pg
              JOIN parcela pc ON pc.id = pg.parcela_id
              JOIN pessoa  ps ON ps.id = pg.pessoa_id
             WHERE pc.despesa_id = ?
             ORDER BY COALESCE(pg.data, pg.criado_em) DESC, pg.id DESC
            """,
            (despesa_id,),
        ).fetchall()


def resumo_pessoas_despesa(despesa_id):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT ps.id, ps.nome, SUM(pg.valor) AS total
              FROM pagamento pg
              JOIN parcela pc ON pc.id = pg.parcela_id
              JOIN pessoa  ps ON ps.id = pg.pessoa_id
             WHERE pc.despesa_id = ?
             GROUP BY ps.id, ps.nome
             ORDER BY total DESC
            """,
            (despesa_id,),
        ).fetchall()


def resumo_pessoas_geral():
    with get_db() as conn:
        return conn.execute(
            """
            SELECT ps.id, ps.nome,
                   COALESCE(SUM(pg.valor), 0) AS total,
                   COUNT(pg.id) AS qtd
              FROM pessoa ps
              LEFT JOIN pagamento pg ON pg.pessoa_id = ps.id
             GROUP BY ps.id, ps.nome
             ORDER BY total DESC, ps.nome COLLATE NOCASE
            """
        ).fetchall()


def totais_gerais():
    with get_db() as conn:
        total = conn.execute("SELECT COALESCE(SUM(valor_total),0) FROM despesa").fetchone()[0]
        pago = conn.execute("SELECT COALESCE(SUM(valor),0) FROM pagamento").fetchone()[0]
        qtd = conn.execute("SELECT COUNT(*) FROM despesa").fetchone()[0]
        return {"total": total, "pago": pago, "saldo": total - pago, "qtd_despesas": qtd}


def extrato_pessoa(pessoa_id):
    with get_db() as conn:
        pessoa = conn.execute("SELECT * FROM pessoa WHERE id = ?", (pessoa_id,)).fetchone()
        por_despesa = conn.execute(
            """
            SELECT d.id, d.nome, SUM(pg.valor) AS total
              FROM pagamento pg
              JOIN parcela pc ON pc.id = pg.parcela_id
              JOIN despesa d  ON d.id = pc.despesa_id
             WHERE pg.pessoa_id = ?
             GROUP BY d.id, d.nome
             ORDER BY total DESC
            """,
            (pessoa_id,),
        ).fetchall()
        lancamentos = conn.execute(
            """
            SELECT pg.*, d.nome AS despesa, d.id AS despesa_id, pc.numero AS parcela_numero
              FROM pagamento pg
              JOIN parcela pc ON pc.id = pg.parcela_id
              JOIN despesa d  ON d.id = pc.despesa_id
             WHERE pg.pessoa_id = ?
             ORDER BY COALESCE(pg.data, pg.criado_em) DESC, pg.id DESC
            """,
            (pessoa_id,),
        ).fetchall()
        total = sum(r["total"] for r in por_despesa)
        return pessoa, por_despesa, lancamentos, total
