# Agente Analista de Campanhas

Automação que lê um relatório de Meta Ads (CSV), detecta anomalias por objetivo
de campanha com regras determinísticas, gera um diagnóstico em linguagem natural
com um LLM (Claude), produz um relatório estruturado (JSON + HTML) e notifica por
Telegram e email. Roda ponta a ponta com um único comando.

> **Regra de ouro:** a camada determinística (Python + pandas) faz **toda** a
> matemática (métricas, baseline, tendência, detecção de anomalia e severidade).
> O LLM **nunca** calcula número — ele só escreve texto e prioriza. Todo número
> do relatório é injetado pelo código.

## Pipeline

```
CSV ─► Ingestão ─► Análise determinística ─► Camada LLM ─► Relatório ─► Notificação
       (limpeza   (anomalias por objetivo,  (diagnóstico   (JSON +     (Telegram +
        + valida)   severidade por regra)    + ação)        HTML)        email)
```

Se o LLM falhar, o pipeline **não aborta**: gera um relatório degradado (só a
parte determinística), com aviso claro, e notifica mesmo assim.

## Requisitos

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) para dependências

## Rodando localmente

```bash
uv sync                       # cria o ambiente a partir do uv.lock
cp .env.example .env          # preencha as chaves que for usar (ver abaixo)
uv run python -m src.main     # roda do CSV à notificação
```

Flags do comando único:

| Flag | Efeito |
|------|--------|
| `--csv CAMINHO` | Sobrescreve o CSV de entrada (default: `data/dados-campanhas.csv`). |
| `--dry-run` | Roda tudo e gera o relatório, mas **não envia** (loga o que sairia). |
| `--no-notify` | Pula a etapa de notificação. |

Saídas geradas em `output/`: `anomalias.json`, `diagnostico.json`,
`relatorio.json` (fonte da verdade) e `relatorio.html`.

Exit code: **0** em sucesso (inclusive com LLM degradado e com falha **parcial**
de notificação — ≥1 canal ok); **não-zero** em qualquer falha fatal (ingestão,
análise ou relatório), erro inesperado, ou quando **todos** os canais de
notificação ligados falharem. cron e CI usam isso para detectar problemas.

### Testes

```bash
uv run pytest
```

## Docker

A imagem é um *batch job*: roda o pipeline uma vez e sai com o código apropriado.

```bash
# build
docker build -t agente-analista-campanhas .

# run (segredos via --env-file; o .env NÃO é embutido na imagem)
docker run --rm --env-file .env agente-analista-campanhas

# variações: passe flags após o nome da imagem
docker run --rm --env-file .env agente-analista-campanhas --dry-run
```

Para extrair os relatórios, monte um volume em `/app/output`:

```bash
docker run --rm --env-file .env -v "$(pwd)/output:/app/output" agente-analista-campanhas
```

> **Linux:** o container roda como usuário não-root, e o bind-mount herda a
> ownership do diretório do host. Se a escrita em `output/` falhar com
> `PermissionError`, alinhe o UID rodando com `--user "$(id -u):$(id -g)"`, ou
> use um volume nomeado (`-v relatorios:/app/output`). No Docker Desktop
> (macOS/Windows) normalmente não é necessário.

## Execução agendada (GitHub Actions)

O workflow [`.github/workflows/daily-analysis.yml`](.github/workflows/daily-analysis.yml)
roda o pipeline de duas formas:

- **Agendado:** cron diário às `11:00 UTC` (08:00 de Brasília). O cron do GitHub
  é em UTC e só dispara na **branch padrão**; execuções podem atrasar alguns
  minutos sob carga.
- **Manual:** `workflow_dispatch` — botão **Run workflow** na aba *Actions*.

Ao final, o relatório (`output/`) é publicado como **artefato** da execução.

As credenciais vêm **exclusivamente de GitHub Secrets** (referenciadas como
`${{ secrets.NOME }}`) — nenhuma chave é escrita no arquivo do workflow.

Os canais ficam **desligados por padrão** e são ligados por **repository
Variables** (não-sensíveis), em *Settings → Secrets and variables → Actions →
Variables*: crie `TELEGRAM_ENABLED=true` e/ou `EMAIL_ENABLED=true` **depois** de
cadastrar os Secrets do canal. Assim a execução agendada não falha enquanto a
configuração ainda não está completa.

### Secrets necessários

Crie em **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Para quê | Obrigatório? |
|--------|----------|--------------|
| `ANTHROPIC_API_KEY` | Diagnóstico em linguagem natural (Claude). | Recomendado. Sem ele, o relatório sai **degradado** (só determinístico) e a execução ainda é **sucesso**. |
| `TELEGRAM_BOT_TOKEN` | Token do bot (via @BotFather). | Sim, se `TELEGRAM_ENABLED=true`. |
| `TELEGRAM_CHAT_ID` | Chat/grupo de destino. | Sim, se `TELEGRAM_ENABLED=true`. |
| `RESEND_API_KEY` | Envio de email via [Resend](https://resend.com). | Sim, se `EMAIL_ENABLED=true` e usando Resend. |
| `EMAIL_FROM` | Remetente do email. | Sim, se `EMAIL_ENABLED=true`. |
| `EMAIL_TO` | Destinatário(s); vários separados por vírgula. | Sim, se `EMAIL_ENABLED=true`. |

**Alternativa ao Resend — SMTP** (opcional; deixe `RESEND_API_KEY` vazio e
descomente o bloco SMTP no workflow):

| Secret | Para quê |
|--------|----------|
| `SMTP_HOST` | Servidor SMTP (ex.: `smtp.gmail.com`). |
| `SMTP_PORT` | Porta (ex.: `587`). |
| `SMTP_USER` | Usuário SMTP. |
| `SMTP_PASSWORD` | Senha de app / SMTP. |

> Segurança: nunca commite `.env` nem chaves. O `.gitignore` cobre `.env`; o
> `.dockerignore` impede o `.env` de entrar na imagem; o workflow só referencia
> Secrets, nunca valores literais.
