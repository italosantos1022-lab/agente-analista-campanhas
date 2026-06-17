# CLAUDE.md — Agente Analista de Campanhas

Contexto do projeto para o Claude Code. Leia este arquivo antes de qualquer tarefa e respeite estas decisões em todas as sessões.

## Objetivo

Automação que lê um relatório de Meta Ads (CSV), detecta anomalias por objetivo de campanha, gera um diagnóstico em linguagem natural com um LLM, produz um relatório estruturado e notifica por Telegram e email. Roda ponta a ponta com um único comando.

## A REGRA DE OURO (não viole)

A camada determinística (Python puro + pandas) faz TODA a matemática: métricas, tendências, baselines e detecção de anomalias por regra. O LLM NUNCA calcula número. Ele recebe as anomalias já detectadas e estruturadas, e só escreve o diagnóstico em texto, define severidade descritiva e prioriza. Todo número que aparece no relatório final é injetado pelo código, não pelo LLM.

Motivo: controla custo, evita alucinação e escala (o LLM sempre vê dezenas de resumos, nunca milhões de linhas cruas).

## Stack (travada, não substituir)

- Python 3.11+
- uv para dependências (`pyproject.toml`)
- pandas: ingestão, limpeza e cálculo
- pydantic v2: validação de schema das linhas e da resposta do LLM
- pydantic-settings + python-dotenv: config tipada via `.env`, falha rápido se faltar chave
- anthropic SDK + Claude (Sonnet no diagnóstico; Haiku como referência de custo)
- tenacity: retry com backoff na chamada ao LLM
- Jinja2: template do relatório HTML
- httpx: chamada à Bot API do Telegram
- Resend (SDK) para email, com smtplib/SMTP como fallback documentado
- logging (stdlib): logs estruturados em cada etapa
- pytest + unittest.mock: testes

Não usar Node, Next ou framework web. Isto é um batch job de dados, não um app web. Sem dependência pesada para tarefa pequena.

## Estrutura de pastas

```
src/
  main.py        # orquestração e CLI (--dry-run, --no-notify)
  config.py      # pydantic-settings, lê o .env
  ingestion.py   # carrega, limpa e valida o CSV
  models.py      # schemas pydantic (linha, anomalia, relatório)
  analysis.py    # métricas, baseline e detecção de anomalia por objetivo
  llm.py         # chamada ao Claude, validação da saída, retry e fallback
  report.py      # monta JSON (fonte da verdade) e HTML (Jinja2)
  notify.py      # interface Notifier + Telegram + Email + dispatcher
templates/
  report.html.j2
tests/
data/dados-campanhas.csv
output/
.env.example
Dockerfile
.github/workflows/daily-analysis.yml
README.md
```

## Dados sujos que a ingestão DEVE tratar

O CSV tem armadilhas plantadas. A camada de ingestão precisa lidar com cada uma e LOGAR o que fez:

1. Campo `gasto` em formato brasileiro `"1.272,60"` (CAMP-004, 04/06), enquanto o resto usa ponto decimal. Normalizar para float.
2. `cliques_link` negativo `-1005` (CAMP-001, 07/06). Valor impossível. Corrigir o sinal ou flag e tratar, nunca usar negativo no cálculo.
3. Linha exatamente duplicada (CAMP-005, 05/06). Deduplicar.
4. Dados de conversão faltando com checkout presente (CAMP-004, 03/06). Marcar como incompleto, não quebrar.
5. `leads = 0` por vários dias (CAMP-003). Proteger divisão por zero E tratar como sinal de anomalia real.
6. Colunas vazias ESPERADAS por objetivo (Reconhecimento não tem compras). Não confundir vazio esperado com dado faltando, senão gera alerta falso.

CSV malformado (arquivo vazio, sem coluna obrigatória, encoding ruim) deve falhar com erro claro, não com stack trace cru.

## Métricas que importam por objetivo

A detecção de anomalia é por objetivo, não a mesma régua pra todos:

- Vendas: ROAS, custo por compra, CTR de link, frequência.
- Leads: custo por lead, volume de leads, CTR.
- Mensagens: custo por conversa, volume de conversas.
- Reconhecimento: frequência, alcance, CPM, CTR. NÃO usar ROAS aqui.

Tipos de anomalia a detectar: custo por resultado acima do baseline da própria campanha, queda brusca de CTR, frequência alta (fadiga de criativo), ROAS em queda, colapso de volume (alcance, leads ou conversas despencando).

Baseline: média da própria campanha na janela de 7 dias. Comparar tendência (primeiro vs último dia e variação percentual).

Severidade: crítica, alta, média. Atribuída por regra na camada determinística.

## Camada LLM

Recebe a lista de anomalias estruturadas e devolve JSON validado por pydantic. Se a resposta vier fora do schema, retry com tenacity. Se a API falhar de vez, o fluxo NÃO quebra: gera relatório degradado (só a parte determinística) com um aviso claro de que o diagnóstico em linguagem natural ficou indisponível.

Temperatura baixa. O LLM só pode recomendar a partir de um conjunto controlado de ações, não inventa métrica nem número.

## Saída

- `output/relatorio.json`: fonte da verdade, estruturado.
- `output/relatorio.html`: resumo executivo, tabela de anomalias com severidade, recomendação por anomalia. Números vêm do código.
- Idioma do relatório: português do Brasil.

## Notificação

Interface `Notifier` com duas implementações. Dispatcher lê do `.env` quais canais estão ligados e envia para todos. Cada canal envia isolado: se um falhar, o outro ainda vai, e a falha fica logada.

- Telegram: versão enxuta (resumo executivo + 2 ou 3 anomalias mais críticas), formatada pra celular.
- Email: relatório HTML completo.

## Execução

Comando único: `uv run python -m src.main`. Flags: `--dry-run` (não notifica), `--no-notify`. Nada de passo manual fora do README.

## Segurança

Nunca commitar `.env` nem chave de API. Tudo via variável de ambiente, documentado no `.env.example`. `.gitignore` cobrindo `.env` e `output/`.

## Ordem de build (não pular fase)

0. Setup: repo, uv, estrutura, config, logging.
1. Ingestão e validação (trata as 6 armadilhas). É a parte que mais pesa em robustez.
2. Análise determinística por objetivo.
3. Camada LLM com retry e fallback.
4. Relatório JSON + HTML.
5. Notificação Telegram + email.
6. Orquestração ponta a ponta.
7. README, dissertativas, vídeo, exemplo de saída.
8. Bônus: testes, Dockerfile, GitHub Actions, estimativa de custo.

Cada camada é construída e testada isolada antes de ligar na próxima.

## Princípios

Simplicidade na medida certa. Justificar cada decisão. Sem excesso de engenharia. Tratar erro em toda fronteira de I/O (arquivo, API, rede). Logar o suficiente para auditar uma execução sem supervisão.