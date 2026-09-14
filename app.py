"""Controle de Despesas da Reforma — app web local (Flask + SQLite).

Uso:
    pip install flask
    python app.py
    abra http://127.0.0.1:5000
"""

import os
import re
import webbrowser
from datetime import date
from threading import Timer

from flask import (Flask, flash, redirect, render_template, request, url_for)

import backup
import db

app = Flask(__name__)
app.secret_key = "reforma-local"


# --------------------------------------------------------------------------
# Helpers de dinheiro (armazenado em centavos)
# --------------------------------------------------------------------------

def parse_money(texto):
    """Aceita '3.000,00', '3000.00', '3000', 'R$ 1.500,50' -> centavos (int)."""
    if texto is None:
        raise ValueError("valor vazio")
    s = str(texto).strip().replace("R$", "").replace(" ", "").replace(" ", "")
    if not s:
        raise ValueError("valor vazio")
    s = re.sub(r"[^\d,.\-]", "", s)
    if "," in s and "." in s:
        # formato BR: ponto = milhar, vírgula = decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    valor = round(float(s) * 100)
    if valor <= 0:
        raise ValueError("valor deve ser maior que zero")
    return int(valor)


def brl(centavos):
    if centavos is None:
        centavos = 0
    sinal = "-" if centavos < 0 else ""
    centavos = abs(int(centavos))
    inteiro, cents = divmod(centavos, 100)
    return f"{sinal}R$ {inteiro:,.0f}".replace(",", ".") + f",{cents:02d}"


def parse_id_opcional(texto):
    texto = (texto or "").strip()
    return int(texto) if texto else None


def parse_data(texto, campo="data"):
    """'AAAA-MM-DD' (como vem do <input type=date>) -> date, ou None se vazio."""
    texto = (texto or "").strip()
    if not texto:
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise ValueError(f"{campo} inválida") from None


def data_br(iso):
    if not iso:
        return "—"
    partes = str(iso)[:10].split("-")
    return "/".join(reversed(partes)) if len(partes) == 3 else iso


# status: (rótulo do filtro, rótulo da tag, classe css) — na ordem dos filtros do painel
STATUS_DESPESA = {
    "quitada": ("Quitadas", "Quitada", "ok"),
    "parcial": ("Parcial", "Parcial", "parcial"),
    "aberta": ("Em aberto", "Em aberto", "aberta"),
}


def status_despesa(despesa):
    if despesa["valor_total"] - despesa["pago"] <= 0:
        return "quitada"
    return "parcial" if despesa["pago"] > 0 else "aberta"


app.jinja_env.filters["brl"] = brl
app.jinja_env.filters["data_br"] = data_br
app.jinja_env.filters["status_despesa"] = status_despesa
app.jinja_env.globals["STATUS_DESPESA"] = STATUS_DESPESA


@app.after_request
def backup_apos_alteracao(response):
    if request.method == "POST":
        try:
            backup.fazer_backup()
        except Exception as e:  # backup nunca pode derrubar o app
            print(f"[backup] falhou: {e}")
    return response


DIAS_AVISO = 7  # parcelas que vencem em até N dias já aparecem como pendência


def calcular_pendencias():
    """Parcelas a abater agrupadas por urgência: vencidas, próximas (até DIAS_AVISO) e futuras."""
    hoje = date.today()
    grupos = {"vencidas": [], "proximas": [], "futuras": []}
    for pc in db.parcelas_pendentes():
        try:
            dias = (date.fromisoformat(pc["vencimento"]) - hoje).days
        except ValueError:
            continue
        item = dict(pc, dias=dias, falta=pc["valor"] - pc["pago"])
        if dias < 0:
            grupos["vencidas"].append(item)
        elif dias <= DIAS_AVISO:
            grupos["proximas"].append(item)
        else:
            grupos["futuras"].append(item)
    return grupos


@app.context_processor
def inject_globals():
    pend = calcular_pendencias()
    acertos_pendentes = [s for s in db.saldos_entre_pessoas() if s["valor"] > 0]
    return {
        "pessoas_ativas": db.listar_pessoas(somente_ativas=True),
        "hoje": date.today().isoformat(),
        "pend": pend,
        "acertos_pendentes": acertos_pendentes,
        "dias_aviso": DIAS_AVISO,
        "qtd_pendencias": len(pend["vencidas"]) + len(pend["proximas"]) + len(acertos_pendentes),
    }


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.route("/")
def index():
    despesas = db.listar_despesas()
    contagem = {chave: 0 for chave in STATUS_DESPESA}
    for d in despesas:
        contagem[status_despesa(d)] += 1
    contagem["todas"] = len(despesas)

    filtro = request.args.get("status")
    if filtro not in STATUS_DESPESA:
        filtro = None
    if filtro:
        despesas = [d for d in despesas if status_despesa(d) == filtro]

    return render_template(
        "index.html",
        totais=db.totais_gerais(),
        despesas=despesas,
        filtro=filtro,
        contagem=contagem,
        por_pessoa=db.resumo_pessoas_geral(),
        saldos=db.saldos_entre_pessoas(),
    )


# --------------------------------------------------------------------------
# Despesas
# --------------------------------------------------------------------------

@app.route("/despesas/nova", methods=["GET", "POST"])
def nova_despesa():
    if request.method == "POST":
        try:
            nome = (request.form.get("nome") or "").strip()
            if not nome:
                raise ValueError("informe o nome da despesa")
            valor_total = parse_money(request.form.get("valor_total"))
            qtd = int(request.form.get("qtd_parcelas") or 1)
            if qtd < 1 or qtd > 120:
                raise ValueError("quantidade de parcelas inválida")

            valores = None
            valor_parcela_txt = (request.form.get("valor_parcela") or "").strip()
            if valor_parcela_txt:
                # o usuário informou o valor da parcela: usa-o e joga a diferença na última
                vp = parse_money(valor_parcela_txt)
                valores = [vp] * qtd
                diff = valor_total - vp * qtd
                valores[-1] += diff
                if valores[-1] <= 0:
                    raise ValueError("valor da parcela incompatível com o valor total")

            pagador_id = parse_id_opcional(request.form.get("pagador_id"))
            primeiro_venc = parse_data(request.form.get("primeiro_vencimento"), "data de vencimento")
            if pagador_id and not primeiro_venc:
                raise ValueError("compras no cartão precisam da data de vencimento da 1ª parcela")

            despesa_id = db.criar_despesa(
                nome=nome,
                valor_total=valor_total,
                qtd_parcelas=qtd,
                categoria=(request.form.get("categoria") or "").strip() or None,
                observacao=(request.form.get("observacao") or "").strip() or None,
                valores_parcelas=valores,
                vencimentos=db.gerar_vencimentos(primeiro_venc, qtd) if primeiro_venc else None,
                pagador_id=pagador_id,
            )
            flash(f"Despesa “{nome}” criada.", "ok")
            return redirect(url_for("detalhe_despesa", despesa_id=despesa_id))
        except ValueError as e:
            flash(f"Erro: {e}", "erro")
    return render_template("nova_despesa.html")


@app.route("/despesas/<int:despesa_id>")
def detalhe_despesa(despesa_id):
    despesa = db.obter_despesa(despesa_id)
    if not despesa:
        flash("Despesa não encontrada.", "erro")
        return redirect(url_for("index"))
    parcelas = db.listar_parcelas(despesa_id)
    pago = sum(p["pago"] for p in parcelas)
    return render_template(
        "despesa.html",
        despesa=despesa,
        parcelas=parcelas,
        pago=pago,
        saldo=despesa["valor_total"] - pago,
        pagamentos=db.listar_pagamentos_despesa(despesa_id),
        por_pessoa=db.resumo_pessoas_despesa(despesa_id),
        todas_pessoas=db.listar_pessoas(),
    )


@app.route("/despesas/<int:despesa_id>/pagador", methods=["POST"])
def definir_pagador(despesa_id):
    try:
        db.definir_pagador(
            despesa_id,
            parse_id_opcional(request.form.get("pagador_id")),
            parse_data(request.form.get("primeiro_vencimento"), "data de vencimento"),
        )
        flash("Cartão da compra atualizado.", "ok")
    except ValueError as e:
        flash(f"Erro: {e}", "erro")
    return redirect(url_for("detalhe_despesa", despesa_id=despesa_id))


@app.route("/despesas/<int:despesa_id>/excluir", methods=["POST"])
def remover_despesa(despesa_id):
    db.excluir_despesa(despesa_id)
    flash("Despesa movida para a lixeira. Dá para restaurar em Lixeira.", "ok")
    return redirect(url_for("index"))


@app.route("/despesas/<int:despesa_id>/restaurar", methods=["POST"])
def restaurar_despesa(despesa_id):
    db.restaurar_despesa(despesa_id)
    flash("Despesa restaurada.", "ok")
    return redirect(url_for("detalhe_despesa", despesa_id=despesa_id))


@app.route("/lixeira")
def lixeira():
    return render_template("lixeira.html", despesas=db.listar_despesas(na_lixeira=True))


@app.route("/pendencias")
def pendencias():
    # parcelas e acertos pendentes já vêm do context processor (pend, acertos_pendentes)
    return render_template("pendencias.html")


@app.route("/parcelas/<int:parcela_id>/editar", methods=["POST"])
def editar_parcela(parcela_id):
    despesa_id = int(request.form.get("despesa_id"))
    try:
        valor = parse_money(request.form.get("valor"))
        venc = parse_data(request.form.get("vencimento"), "data de vencimento")
        despesa = db.obter_despesa(despesa_id)
        if not venc and despesa and despesa["pagador_id"]:
            raise ValueError("compras no cartão precisam de data de vencimento em todas as parcelas")
        ajustar = bool(request.form.get("ajustar_seguintes")) and venc is not None
        db.atualizar_parcela(parcela_id, valor, venc.isoformat() if venc else None,
                             ajustar_seguintes=ajustar)
        flash("Parcela atualizada e parcelas seguintes reajustadas." if ajustar
              else "Parcela atualizada.", "ok")
    except ValueError as e:
        flash(f"Erro: {e}", "erro")
    return redirect(url_for("detalhe_despesa", despesa_id=despesa_id))


# --------------------------------------------------------------------------
# Pagamentos
# --------------------------------------------------------------------------

@app.route("/pagamentos/novo", methods=["POST"])
def novo_pagamento():
    despesa_id = int(request.form.get("despesa_id"))
    try:
        parcela_id = int(request.form.get("parcela_id"))
        pessoa_id = int(request.form.get("pessoa_id"))
        valor = parse_money(request.form.get("valor"))
        db.criar_pagamento(
            parcela_id=parcela_id,
            pessoa_id=pessoa_id,
            valor=valor,
            data=(request.form.get("data") or "").strip() or None,
            observacao=(request.form.get("observacao") or "").strip() or None,
        )
        flash("Pagamento registrado.", "ok")
    except (ValueError, TypeError) as e:
        flash(f"Erro ao registrar pagamento: {e}", "erro")
    return redirect(url_for("detalhe_despesa", despesa_id=despesa_id))


@app.route("/pagamentos/<int:pagamento_id>/excluir", methods=["POST"])
def remover_pagamento(pagamento_id):
    despesa_id = db.excluir_pagamento(pagamento_id)
    flash("Pagamento removido.", "ok")
    if despesa_id:
        return redirect(url_for("detalhe_despesa", despesa_id=despesa_id))
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Pessoas
# --------------------------------------------------------------------------

@app.route("/pessoas", methods=["GET", "POST"])
def pessoas():
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        if nome:
            try:
                db.criar_pessoa(nome)
                flash(f"Pessoa “{nome}” cadastrada.", "ok")
            except Exception:
                flash("Já existe uma pessoa com esse nome.", "erro")
        return redirect(url_for("pessoas"))
    return render_template(
        "pessoas.html",
        pessoas=db.listar_pessoas(),
        resumo=db.resumo_pessoas_geral(),
    )


@app.route("/pessoas/<int:pessoa_id>/alternar", methods=["POST"])
def alternar_pessoa(pessoa_id):
    db.alternar_pessoa(pessoa_id)
    return redirect(url_for("pessoas"))


@app.route("/pessoas/<int:pessoa_id>/excluir", methods=["POST"])
def remover_pessoa(pessoa_id):
    if db.excluir_pessoa(pessoa_id):
        flash("Pessoa excluída.", "ok")
    else:
        flash("Não dá para excluir: essa pessoa já tem pagamentos, compras no cartão ou acertos. "
              "Desative-a.", "erro")
    return redirect(url_for("pessoas"))


@app.route("/pessoas/<int:pessoa_id>")
def detalhe_pessoa(pessoa_id):
    pessoa, por_despesa, lancamentos, total = db.extrato_pessoa(pessoa_id)
    if not pessoa:
        flash("Pessoa não encontrada.", "erro")
        return redirect(url_for("pessoas"))
    return render_template(
        "pessoa.html",
        pessoa=pessoa,
        por_despesa=por_despesa,
        lancamentos=lancamentos,
        total=total,
        saldos=db.saldos_entre_pessoas(pessoa_id),
    )


# --------------------------------------------------------------------------
# Acertos (quem deve para quem)
# --------------------------------------------------------------------------

@app.route("/acertos", methods=["GET", "POST"])
def acertos():
    if request.method == "POST":
        try:
            de = int(request.form.get("de_pessoa_id"))
            para = int(request.form.get("para_pessoa_id"))
            if de == para:
                raise ValueError("escolha duas pessoas diferentes")
            db.criar_acerto(
                de_pessoa_id=de,
                para_pessoa_id=para,
                valor=parse_money(request.form.get("valor")),
                data=(request.form.get("data") or "").strip() or None,
                observacao=(request.form.get("observacao") or "").strip() or None,
            )
            flash("Acerto registrado.", "ok")
        except (ValueError, TypeError) as e:
            flash(f"Erro ao registrar acerto: {e}", "erro")
        return redirect(url_for("acertos"))
    return render_template(
        "acertos.html",
        saldos=db.saldos_entre_pessoas(),
        origens=db.dividas_por_despesa(),
        lista=db.listar_acertos(),
        todas_pessoas=db.listar_pessoas(),
        sugestao=request.args,
    )


@app.route("/acertos/<int:acerto_id>/excluir", methods=["POST"])
def remover_acerto(acerto_id):
    db.excluir_acerto(acerto_id)
    flash("Acerto removido.", "ok")
    return redirect(url_for("acertos"))


def abrir_navegador():
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    backup.fazer_backup()  # antes do init_db, para guardar o banco antes de qualquer migração
    backup.enviar_para_github()  # envia backups que ficaram sem push (ex.: app fechado sem internet)
    db.init_db()
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and os.environ.get("REFORMA_NO_BROWSER") != "1":
        Timer(1.0, abrir_navegador).start()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
