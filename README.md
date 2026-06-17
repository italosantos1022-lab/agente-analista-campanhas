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

## Decisões de arquitetura

**Separação determinística / LLM.** É a decisão central. O LLM é bom em linguagem
e síntese e ruim em aritmética, além de propenso a alucinar. Por isso toda conta
é determinística e verificável, e o LLM só narra e prioriza. Isso traz três
ganhos: controla custo (o LLM vê um punhado de anomalias já resumidas, nunca as
linhas cruas), elimina o risco de um número inventado chegar ao cliente, e escala
(o volume de chamadas ao LLM acompanha o número de campanhas, não o de linhas).

**Camada LLM agnóstica de provedor.** O LLM vive atrás de um único módulo. Trocar
de provedor mexe só nesse módulo, sem encostar na lógica de análise — algo
comprovado na prática ao trocar de provedor durante o desenvolvimento. A
inteligência está na camada determinística; o LLM é um componente fino e
substituível.

**Python e pandas.** O núcleo da tarefa é limpar dados tabulares sujos e calcular
estatísticas por objetivo, terreno nativo do pandas. Isto é um *batch job* de
dados, não uma aplicação web; um framework web seria a ferramenta errada.

**Anomalia por objetivo.** Nem toda métrica importa para todo objetivo. Uma
campanha de Reconhecimento é avaliada por frequência, alcance e CTR, não por
ROAS; uma de Mensagens, por custo por conversa. As regras codificam essa
diferença em vez de aplicar uma régua única.

**Baseline relativo à própria campanha.** Cada anomalia é medida contra a média e
a tendência da própria campanha na janela, não contra outras campanhas. Uma
campanha de Black Friday com ROAS 9 é saudável no seu próprio patamar mesmo que
outra rode a 15. Isso evita alarme falso por comparação cruzada.

**Dois métodos de comparação, por natureza da métrica.** Métricas direcionais
(ROAS) usam tendência (primeiro vs último dia); métricas de nível (custo por
resultado) usam desvio em relação ao baseline. Cada métrica usa a comparação que
faz sentido para ela.

**Guardrails contra alucinação.** A saída do LLM é validada contra um schema
(pydantic); resposta fora do schema é rejeitada e re-tentada. O LLM só pode citar
anomalias que existem na camada determinística (qualquer outra é descartada) e só
pode recomendar ações de um conjunto fechado. Uma auditoria varre o texto do LLM
para garantir que nenhum número fabricado escapou. No relatório final, os números
vêm do código e o texto do LLM é escapado antes de ser renderizado.

**Notificação isolada por canal.** Cada canal envia de forma independente: se um
falha, o outro ainda sai, e a falha fica logada. O formato se adapta ao canal
(mensagem enxuta no Telegram, HTML completo no email).

**Exit codes pensados para execução sem supervisão.** `0` para sucesso (inclusive
LLM degradado e falha parcial de notificação); não-zero para erro fatal de
pipeline e para falha total de notificação, para que um agendador saiba quando
nenhum alerta chegou a ninguém.

## Validação e dados sujos

Dados reais raramente vêm limpos. A ingestão trata e **loga** cada caso:

- Número em formato brasileiro (`"1.272,60"`) normalizado para float.
- Clique negativo corrigido (impossível por definição; ver *Limitações*).
- Linha duplicada removida.
- Dia com dado de conversão faltando marcado como incompleto: excluído do cálculo
  para não distorcer, e sinalizado na seção de qualidade do relatório.
- `leads = 0` preservado como valor real (é sinal de anomalia, não dado faltando),
  com proteção contra divisão por zero no custo por lead.
- Campos vazios **esperados** por objetivo distinguidos de dados faltando, para
  não gerar alarme falso (uma campanha de Reconhecimento não tem coluna de
  compras).
- CSV malformado (vazio, sem coluna obrigatória) falha com erro claro, não com
  *stack trace* cru.

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

## Limitações conhecidas

Decisões conscientes de escopo. Para este teste optei pelo caminho simples; ao
lado de cada uma está o que faria em produção.

- **Thresholds são percentuais fixos**, calibrados à mão para sair do ruído
  diário. Funcionam nesta base, mas em produção usaria banda estatística
  (desvios-padrão da média da própria campanha), que se adapta por campanha e
  vertical sem ajuste manual.
- **A correção de clique negativo usa valor absoluto.** Aqui é seguro porque o CPC
  e o CTR do mesmo dia corroboram a magnitude. Em produção, o ideal seria flagar
  para revisão em vez de corrigir cegamente, ou só corrigir quando um campo
  vizinho confirma o valor.
- **A entrada é um CSV fixo.** Num cenário real os dados viriam da API do Meta ou
  de um export agendado, e a ingestão precisaria de paginação e tratamento de
  janelas de data.
- **Sem persistência histórica.** Cada execução é independente; o baseline é a
  janela de 7 dias do próprio arquivo, sem memória entre dias nem consciência de
  sazonalidade (fim de semana, datas comerciais).
- **Uma única chamada ao LLM por execução.** Para muitos clientes simultâneos
  seria preciso agregar por cliente e paralelizar. A arquitetura já favorece isso
  (o LLM só vê resumos), mas a paralelização não está implementada.