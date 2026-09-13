# 🧱 Controle de Despesas da Reforma

App web que roda **só na sua máquina** (Flask + SQLite). Sem nuvem, sem conta, sem internet.
Os dados ficam num arquivo `reforma.db` na mesma pasta do projeto.

## Como rodar

Requisito: Python 3.9+.

**Linux / macOS**

```bash
cd reforma
./iniciar.sh
```

**Windows** — dê dois cliques em `iniciar.bat`.

> Se aparecer *"Python não foi encontrado"*, é porque o Python não está instalado
> (o que o Windows mostra é só um atalho para a Microsoft Store). Instale com
> `winget install -e --id Python.Python.3.12` no PowerShell, ou baixe em
> <https://www.python.org/downloads/windows/> marcando **"Add python.exe to PATH"**
> na primeira tela do instalador. Depois rode o `iniciar.bat` de novo.

**Manual (qualquer sistema)**

```bash
pip install flask
python app.py
```

O navegador abre sozinho em <http://127.0.0.1:5000>. Para parar: `Ctrl+C` no terminal.

## Como usar

1. **Pessoas** → cadastre quem participa (Raphael, Elaine, ...).
2. **Nova despesa** → ex.: `Pintor`, valor `3000,00`, `2` parcelas.
   O sistema cria as parcelas de R$ 1.500,00 automaticamente.
   Se as parcelas não forem iguais, é só usar o link *editar* de cada parcela depois.
3. Na tela da despesa, use **Registrar pagamento**: escolha a parcela, quem pagou,
   o valor abatido e (opcional) a data e uma observação.
   Ex.: Raphael R$ 750,00 + Elaine R$ 750,00 na parcela 1 → parcela fica **Paga**.
4. Se a compra foi no cartão de alguém, veja em **Acertos** quem deve para quem.
5. Os totais aparecem em três lugares:
   - **Tela da despesa** — quanto cada pessoa pagou nela e quanto falta abater.
   - **Tela da pessoa** — total pago no geral e quebrado por despesa.
   - **Painel** — total da reforma, total pago, total a pagar e ranking por pessoa.
     A lista de despesas pode ser filtrada por **Quitadas**, **Parcial** ou **Em aberto**.

Valores aceitam `3000`, `3000.00` ou `3.000,00` — tanto faz.
A data é opcional em todo pagamento.

## Estrutura

```
reforma/
├── app.py            # rotas Flask e formatação (R$, datas)
├── db.py             # schema e consultas SQLite
├── backup.py         # backup automático (cópia datada + commit no git + OneDrive)
├── backups/          # cópias datadas do banco (versionadas no git)
├── templates/        # telas (Jinja2)
├── static/style.css  # estilo
├── requirements.txt
└── reforma.db        # criado no primeiro uso — é o seu backup, copie quando quiser
```

Valores são guardados em **centavos (inteiro)**, então não existe erro de arredondamento.

## Compras no cartão e acertos

Ao criar uma despesa, informe **quem passou o cartão** (dá para alterar depois na tela da despesa).
Quem abater parcelas dessa compra passa a **dever esse valor ao dono do cartão**.
Abatimentos do próprio dono do cartão, ou de despesas sem cartão, não geram dívida.

A tela **Acertos** mostra o saldo já simplificado de cada par de pessoas:
se Raphael deve R$ 1.000 para Elaine e Elaine deve R$ 800 para Raphael, aparece só
*Raphael deve R$ 200 para Elaine*. Quando alguém transferir o dinheiro, registre um
**acerto** (de quem, para quem, valor) e o saldo diminui. Despesas na lixeira não geram dívida.

## Vencimentos e pendências

Compras no cartão exigem a **data de vencimento da 1ª parcela**; as seguintes são geradas
automaticamente, mês a mês, no mesmo dia (dia 31 vira o último dia dos meses mais curtos).
Cada data pode ser editada na tela da despesa — marque *ajustar as parcelas seguintes* para
recalcular as próximas a partir da data editada.

O menu **Pendências** (com contador) e o Painel avisam:
- parcelas **vencidas** ou que vencem nos **próximos 7 dias** e ainda não foram totalmente
  abatidas — o botão *Abater* abre a despesa com a parcela já selecionada;
- **acertos pendentes** entre as pessoas.

Compras no cartão cadastradas antes desta versão, sem datas, receberam **10/09/2026** na
1ª parcela (e os meses seguintes nas demais). Ajuste conforme a fatura real.

## Backup automático

Toda vez que o app abre e depois de cada alteração, uma cópia do banco é salva em
`backups/reforma-AAAA-MM-DD.db` (uma por dia) e **commitada no git** — então cada
versão fica no histórico, mesmo que a cópia do dia seja sobrescrita.
Se nada mudou desde o último backup, nada é feito.

Se existir OneDrive na máquina, a cópia também vai para
`OneDrive\Backups\spend-control` (proteção contra perda do disco).

- **Restaurar**: com o app parado, copie o backup desejado por cima do `reforma.db`.
  Versões antigas do mesmo dia: `git log -- backups/` e `git show <commit>:backups/<arquivo> > reforma.db`.
- **Outra pasta de nuvem**: `REFORMA_BACKUP_NUVEM=D:\MeusBackups`; para desativar: `REFORMA_BACKUP_NUVEM=0`.
- Não coloque a pasta do projeto dentro do OneDrive/Dropbox: sincronizar o `reforma.db`
  com o app aberto pode corrompê-lo. Os backups podem ir, o banco vivo não.

## Lixeira

Excluir uma despesa só a manda para a **Lixeira** (menu no topo): ela sai dos totais,
mas as parcelas e pagamentos continuam guardados e dá para restaurar a qualquer momento.

## Dicas

- **Recomeçar do zero**: renomeie o `reforma.db` (ex.: `reforma-antigo.db`) e rode de novo.
  Evite apagar — os backups continuam em `backups/`, mas é melhor não arriscar.
- **Mudar a porta**: `PORT=5050 python app.py`.
- **Não abrir o navegador sozinho**: `REFORMA_NO_BROWSER=1 python app.py`.
- Uma pessoa que já tem pagamentos não pode ser excluída — **desative** que ela some dos
  formulários mas o histórico continua intacto.
