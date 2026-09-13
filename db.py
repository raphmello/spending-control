"""Camada de acesso ao banco (SQLite).

Todos os valores monetarios sao armazenados em CENTAVOS (INTEGER),
para evitar erros de arredondamento de ponto flutuante.
"""

import calendar
import os
import sqlite3
from contextlib import contextmanager
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("REFORMA_DB", os.path.join(BASE_DIR, "reforma.db"))

# compras no cartão antigas, sem vencimento, recebem esta data na 1ª parcela (migração v1)
VENCIMENTO_INICIAL_MIGRACAO = date(2026, 9, 10)

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
    criado_em     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    excluido_em   TEXT,                      -- preenchido = está na lixeira
    pagador_id    INTEGER REFERENCES pessoa(id)  -- quem passou o cartão (opcional)
);

CREATE TABLE IF NOT EXISTS parcela (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    despesa_id  INTEGER NOT NULL REFERENCES despesa(id) ON DELETE CASCADE,
    numero      INTEGER NOT NULL,
    valor       INTEGER NOT NULL,            -- centavos
    vencimento  TEXT,                        -- YYYY-MM-DD (obrigatório em compras no cartão)
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

-- Transferência entre pessoas para quitar o que uma deve à outra.
CREATE TABLE IF NOT EXISTS acerto (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    de_pessoa_id    INTEGER NOT NULL REFERENCES pessoa(id),
    para_pessoa_id  INTEGER NOT NULL REFERENCES pessoa(id),
    valor           INTEGER NOT NULL,        -- centavos
    data            TEXT,                    -- YYYY-MM-DD (opcional)
    observacao      TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK (de_pessoa_id <> para_pessoa_id),
    CHECK (valor > 0)
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
        # migrações de bancos criados antes destas colunas existirem
        colunas = [r["name"] for r in conn.execute("PRAGMA table_info(despesa)")]
        if "excluido_em" not in colunas:
            conn.execute("ALTER TABLE despesa ADD COLUMN excluido_em TEXT")
        if "pagador_id" not in colunas:
            conn.execute("ALTER TABLE despesa ADD COLUMN pagador_id INTEGER REFERENCES pessoa(id)")

        # v1: compras no cartão passam a exigir vencimento; as antigas sem data recebem
        # VENCIMENTO_INICIAL_MIGRACAO na 1ª parcela (e os meses seguintes nas demais)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            _preencher_vencimentos_vazios(conn, VENCIMENTO_INICIAL_MIGRACAO)
            conn.execute("PRAGMA user_version = 1")


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
    """Só exclui se a pessoa não tiver pagamentos, compras no cartão ou acertos."""
    with get_db() as conn:
        usada = conn.execute(
            """SELECT (SELECT COUNT(*) FROM pagamento WHERE pessoa_id = :id)
                    + (SELECT COUNT(*) FROM despesa WHERE pagador_id = :id)
                    + (SELECT COUNT(*) FROM acerto WHERE de_pessoa_id = :id OR para_pessoa_id = :id)""",
            {"id": pessoa_id},
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


def somar_meses(data, meses):
    """Mesmo dia `meses` meses depois; dia 31 vira o último dia de meses mais curtos."""
    total = data.month - 1 + meses
    ano, mes = data.year + total // 12, total % 12 + 1
    return date(ano, mes, min(data.day, calendar.monthrange(ano, mes)[1]))


def gerar_vencimentos(primeiro, qtd):
    """{número da parcela: 'AAAA-MM-DD'}, mês a mês a partir do vencimento da 1ª parcela."""
    return {i: somar_meses(primeiro, i - 1).isoformat() for i in range(1, qtd + 1)}


def _preencher_vencimentos_vazios(conn, primeiro, despesa_id=None):
    """Preenche parcelas sem vencimento (de uma despesa, ou de todas as compras no cartão),
    tratando `primeiro` como o vencimento da parcela 1."""
    if despesa_id is None:
        filtro, params = "d.pagador_id IS NOT NULL", ()
    else:
        filtro, params = "d.id = ?", (despesa_id,)
    vazias = conn.execute(
        f"""SELECT pc.id, pc.numero
              FROM parcela pc
              JOIN despesa d ON d.id = pc.despesa_id
             WHERE pc.vencimento IS NULL AND {filtro}""",
        params,
    ).fetchall()
    for pc in vazias:
        conn.execute(
            "UPDATE parcela SET vencimento = ? WHERE id = ?",
            (somar_meses(primeiro, pc["numero"] - 1).isoformat(), pc["id"]),
        )
    return len(vazias)


def criar_despesa(nome, valor_total, qtd_parcelas, categoria=None,
                  observacao=None, valores_parcelas=None, vencimentos=None, pagador_id=None):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO despesa (nome, categoria, observacao, valor_total, qtd_parcelas, pagador_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (nome.strip(), categoria, observacao, valor_total, qtd_parcelas, pagador_id),
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
    """Move a despesa para a lixeira (não apaga nada)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE despesa SET excluido_em = datetime('now','localtime') "
            "WHERE id = ? AND excluido_em IS NULL",
            (despesa_id,),
        )


def definir_pagador(despesa_id, pagador_id, primeiro_vencimento=None):
    """Define quem passou o cartão. Compras no cartão exigem vencimento em todas as
    parcelas: as que estiverem sem data são geradas a partir de `primeiro_vencimento`."""
    with get_db() as conn:
        if pagador_id:
            sem_data = conn.execute(
                "SELECT COUNT(*) FROM parcela WHERE despesa_id = ? AND vencimento IS NULL",
                (despesa_id,),
            ).fetchone()[0]
            if sem_data:
                if not primeiro_vencimento:
                    raise ValueError("esta despesa tem parcelas sem vencimento: "
                                     "informe o vencimento da 1ª parcela")
                _preencher_vencimentos_vazios(conn, primeiro_vencimento, despesa_id=despesa_id)
        conn.execute("UPDATE despesa SET pagador_id = ? WHERE id = ?", (pagador_id, despesa_id))


def restaurar_despesa(despesa_id):
    with get_db() as conn:
        conn.execute("UPDATE despesa SET excluido_em = NULL WHERE id = ?", (despesa_id,))


def atualizar_parcela(parcela_id, valor, vencimento, ajustar_seguintes=False):
    """Atualiza o valor/vencimento de uma parcela e sincroniza o total da despesa.

    Com `ajustar_seguintes`, as parcelas de número maior passam a vencer mês a mês
    a partir do novo vencimento (AAAA-MM-DD).
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT despesa_id, numero FROM parcela WHERE id = ?", (parcela_id,)
        ).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE parcela SET valor = ?, vencimento = ? WHERE id = ?",
            (valor, vencimento, parcela_id),
        )
        if ajustar_seguintes and vencimento:
            base = date.fromisoformat(vencimento)
            seguintes = conn.execute(
                "SELECT id, numero FROM parcela WHERE despesa_id = ? AND numero > ?",
                (row["despesa_id"], row["numero"]),
            ).fetchall()
            for pc in seguintes:
                conn.execute(
                    "UPDATE parcela SET vencimento = ? WHERE id = ?",
                    (somar_meses(base, pc["numero"] - row["numero"]).isoformat(), pc["id"]),
                )
        conn.execute(
            """UPDATE despesa
                  SET valor_total = (SELECT COALESCE(SUM(valor),0) FROM parcela WHERE despesa_id = ?)
                WHERE id = ?""",
            (row["despesa_id"], row["despesa_id"]),
        )


def obter_despesa(despesa_id):
    with get_db() as conn:
        return conn.execute(
            """SELECT d.*, ps.nome AS pagador
                 FROM despesa d
                 LEFT JOIN pessoa ps ON ps.id = d.pagador_id
                WHERE d.id = ?""",
            (despesa_id,),
        ).fetchone()


def listar_despesas(na_lixeira=False):
    """Despesas com total pago e saldo (ativas, ou só as da lixeira)."""
    filtro = "d.excluido_em IS NOT NULL" if na_lixeira else "d.excluido_em IS NULL"
    ordem = "d.excluido_em DESC" if na_lixeira else "d.criado_em DESC"
    with get_db() as conn:
        return conn.execute(
            f"""
            SELECT d.*,
                   COALESCE((
                       SELECT SUM(pg.valor)
                         FROM pagamento pg
                         JOIN parcela pc ON pc.id = pg.parcela_id
                        WHERE pc.despesa_id = d.id
                   ), 0) AS pago,
                   ps.nome AS pagador
              FROM despesa d
              LEFT JOIN pessoa ps ON ps.id = d.pagador_id
             WHERE {filtro}
             ORDER BY {ordem}, d.id DESC
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


def parcelas_pendentes():
    """Parcelas com vencimento e valor ainda não totalmente abatido (fora da lixeira),
    da mais antiga para a mais nova."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM (
                SELECT pc.id, pc.numero, pc.valor, pc.vencimento,
                       d.id AS despesa_id, d.nome AS despesa, d.qtd_parcelas,
                       ps.nome AS pagador,
                       COALESCE((SELECT SUM(valor) FROM pagamento WHERE parcela_id = pc.id), 0) AS pago
                  FROM parcela pc
                  JOIN despesa d     ON d.id = pc.despesa_id
                  LEFT JOIN pessoa ps ON ps.id = d.pagador_id
                 WHERE d.excluido_em IS NULL AND pc.vencimento IS NOT NULL
            )
             WHERE pago < valor
             ORDER BY vencimento, despesa, numero
            """
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
              LEFT JOIN (
                    SELECT p.id, p.pessoa_id, p.valor
                      FROM pagamento p
                      JOIN parcela pc ON pc.id = p.parcela_id
                      JOIN despesa d  ON d.id = pc.despesa_id
                     WHERE d.excluido_em IS NULL
                   ) pg ON pg.pessoa_id = ps.id
             GROUP BY ps.id, ps.nome
             ORDER BY total DESC, ps.nome COLLATE NOCASE
            """
        ).fetchall()


def totais_gerais():
    with get_db() as conn:
        total, qtd = conn.execute(
            "SELECT COALESCE(SUM(valor_total),0), COUNT(*) FROM despesa WHERE excluido_em IS NULL"
        ).fetchone()
        pago = conn.execute(
            """SELECT COALESCE(SUM(pg.valor),0)
                 FROM pagamento pg
                 JOIN parcela pc ON pc.id = pg.parcela_id
                 JOIN despesa d  ON d.id = pc.despesa_id
                WHERE d.excluido_em IS NULL"""
        ).fetchone()[0]
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
             WHERE pg.pessoa_id = ? AND d.excluido_em IS NULL
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
             WHERE pg.pessoa_id = ? AND d.excluido_em IS NULL
             ORDER BY COALESCE(pg.data, pg.criado_em) DESC, pg.id DESC
            """,
            (pessoa_id,),
        ).fetchall()
        total = sum(r["total"] for r in por_despesa)
        return pessoa, por_despesa, lancamentos, total


# --------------------------------------------------------------------------
# Acertos: quem deve para quem
#
# Quem abate parcela de uma compra feita no cartão de outra pessoa passa a
# dever esse valor ao dono do cartão. Acertos (transferências entre pessoas)
# abatem essa dívida. Para cada par de pessoas o saldo é simplificado:
# se A deve 1000 a B e B deve 800 a A, o resultado é "A deve 200 a B".
# Despesas na lixeira não geram dívida.
# --------------------------------------------------------------------------

def criar_acerto(de_pessoa_id, para_pessoa_id, valor, data=None, observacao=None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO acerto (de_pessoa_id, para_pessoa_id, valor, data, observacao)
               VALUES (?, ?, ?, ?, ?)""",
            (de_pessoa_id, para_pessoa_id, valor, data or None, observacao or None),
        )


def excluir_acerto(acerto_id):
    with get_db() as conn:
        conn.execute("DELETE FROM acerto WHERE id = ?", (acerto_id,))


def listar_acertos():
    with get_db() as conn:
        return conn.execute(
            """
            SELECT a.*, de.nome AS de_pessoa, para.nome AS para_pessoa
              FROM acerto a
              JOIN pessoa de   ON de.id = a.de_pessoa_id
              JOIN pessoa para ON para.id = a.para_pessoa_id
             ORDER BY COALESCE(a.data, a.criado_em) DESC, a.id DESC
            """
        ).fetchall()


def dividas_por_despesa():
    """De onde vem cada dívida: quanto cada pessoa abateu no cartão de outra, por despesa."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT dev.nome AS devedor, cred.nome AS credor,
                   d.id AS despesa_id, d.nome AS despesa, SUM(pg.valor) AS total
              FROM pagamento pg
              JOIN parcela pc  ON pc.id = pg.parcela_id
              JOIN despesa d   ON d.id = pc.despesa_id
              JOIN pessoa dev  ON dev.id = pg.pessoa_id
              JOIN pessoa cred ON cred.id = d.pagador_id
             WHERE d.excluido_em IS NULL AND d.pagador_id <> pg.pessoa_id
             GROUP BY pg.pessoa_id, d.pagador_id, d.id
             ORDER BY dev.nome, cred.nome, total DESC
            """
        ).fetchall()


def saldos_entre_pessoas(pessoa_id=None):
    """Saldo simplificado de cada par de pessoas com movimento.

    Cada item traz devedor/credor (nomes e ids), `valor` (o que o devedor ainda
    deve; 0 = quites) e os componentes do cálculo:
      compras_devedor  abatido pelo devedor em compras no cartão do credor
      compras_credor   abatido pelo credor em compras no cartão do devedor
      acertos_devedor  já transferido do devedor para o credor
      acertos_credor   já transferido do credor para o devedor
    valor = compras_devedor - compras_credor - acertos_devedor + acertos_credor
    """
    with get_db() as conn:
        nomes = {r["id"]: r["nome"] for r in conn.execute("SELECT id, nome FROM pessoa")}
        compras = conn.execute(
            """
            SELECT pg.pessoa_id AS de, d.pagador_id AS para, SUM(pg.valor) AS total
              FROM pagamento pg
              JOIN parcela pc ON pc.id = pg.parcela_id
              JOIN despesa d  ON d.id = pc.despesa_id
             WHERE d.excluido_em IS NULL
               AND d.pagador_id IS NOT NULL
               AND d.pagador_id <> pg.pessoa_id
             GROUP BY pg.pessoa_id, d.pagador_id
            """
        ).fetchall()
        acertos = conn.execute(
            """SELECT de_pessoa_id AS de, para_pessoa_id AS para, SUM(valor) AS total
                 FROM acerto GROUP BY de_pessoa_id, para_pessoa_id"""
        ).fetchall()

    # pares[(a, b)] com a < b; mov["compras"][x] = quanto x deve ao outro por compras
    pares = {}

    def par(de, para):
        chave = (min(de, para), max(de, para))
        return pares.setdefault(chave, {"compras": {de: 0, para: 0}, "acertos": {de: 0, para: 0}})

    for r in compras:
        par(r["de"], r["para"])["compras"][r["de"]] += r["total"]
    for r in acertos:
        par(r["de"], r["para"])["acertos"][r["de"]] += r["total"]

    saldos = []
    for (a, b), mov in pares.items():
        if pessoa_id is not None and pessoa_id not in (a, b):
            continue
        a_deve_b = mov["compras"][a] - mov["compras"][b] - mov["acertos"][a] + mov["acertos"][b]
        devedor, credor = (a, b) if a_deve_b >= 0 else (b, a)
        saldos.append({
            "devedor_id": devedor, "devedor": nomes[devedor],
            "credor_id": credor, "credor": nomes[credor],
            "valor": abs(a_deve_b),
            "compras_devedor": mov["compras"][devedor],
            "compras_credor": mov["compras"][credor],
            "acertos_devedor": mov["acertos"][devedor],
            "acertos_credor": mov["acertos"][credor],
        })
    saldos.sort(key=lambda s: (-s["valor"], s["devedor"], s["credor"]))
    return saldos
