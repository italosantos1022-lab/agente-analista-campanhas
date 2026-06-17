"""Camada LLM (Fase 3). Diagnóstico em linguagem natural a partir das anomalias.

REGRA DE OURO (CLAUDE.md): o LLM NÃO calcula nem inventa número. Toda a
matemática já foi feita na Fase 2; aqui o Claude só escreve TEXTO (resumo
executivo, narrativa por campanha, diagnóstico e ação por anomalia) e prioriza.
Os números do relatório final vêm sempre do JSON da Fase 2, injetados por código.

Garantias desta camada:
  - Saída do LLM validada por pydantic (`DiagnosticoLLM`); fora do schema -> retry.
  - Ações restritas a um conjunto fechado (enum `AcaoRecomendada`); fora -> retry.
  - Anti-alucinação: diagnóstico/narrativa que cite campanha ou métrica inexistente
    na entrada é DESCARTADO (e logado).
  - `tenacity`: retry com backoff em falha de API ou JSON/schema inválido.
  - Fallback: se a API falhar de vez, gera relatório DEGRADADO (só a parte
    determinística) com aviso claro — o fluxo não quebra.

Entrada: o dicionário do `output/anomalias.json` (Fase 2).
Execução isolada:
    uv run python -m src.llm
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

import anthropic
from pydantic import ValidationError
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Settings, get_settings
from src.logging_config import setup_logging
from src.models import AcaoRecomendada, DiagnosticoLLM

log = logging.getLogger("llm")

# Erros que disparam retry: falha de API OU resposta fora do schema/JSON inválido.
_ERROS_RETRY = (
    anthropic.AnthropicError,
    json.JSONDecodeError,
    ValidationError,
    ValueError,
)

_ACOES = ", ".join(a.value for a in AcaoRecomendada)

SYSTEM_PROMPT = f"""\
Você é um analista sênior de tráfego pago. Sua tarefa é INTERPRETAR anomalias de \
campanhas de Meta Ads que JÁ foram detectadas e quantificadas por uma camada \
determinística. Você escreve em português do Brasil, de forma sóbria e direta.

REGRAS INVIOLÁVEIS:
1. Você NUNCA calcula, estima ou inventa números. NÃO escreva números, \
percentuais, valores monetários ou datas no seu texto — eles já foram calculados \
e serão inseridos automaticamente pelo código. Escreva apenas a leitura qualitativa \
do que está acontecendo.
2. Use APENAS as campanhas e as métricas presentes na entrada. NUNCA invente, \
renomeie ou cite campanha/métrica que não esteja na entrada.
3. Para cada anomalia, recomende EXATAMENTE UMA ação, escolhida SOMENTE deste \
conjunto fechado: {_ACOES}. Não invente ação fora desta lista.
4. Consolide as anomalias de cada campanha numa narrativa curta que conte a história \
da campanha (ex.: "CAMP-002 em saturação crítica: retorno desabando enquanto o \
público se esgota").
5. Priorize: o resumo executivo destaca primeiro o que é crítico.

Responda SOMENTE com um objeto JSON válido (sem markdown, sem comentários, sem texto \
fora do JSON), exatamente neste formato:
{{
  "resumo_executivo": "<2 a 4 frases, priorizando o crítico>",
  "narrativas": [
    {{"campanha_id": "<id existente na entrada>", "narrativa": "<consolida as anomalias da campanha>"}}
  ],
  "diagnosticos": [
    {{"ref": <índice da anomalia na entrada>, "campanha_id": "<id>", "metrica": "<métrica>",
      "diagnostico": "<o que está acontecendo, qualitativo>",
      "acao": "<uma ação da lista>", "justificativa_acao": "<por que essa ação>"}}
  ]
}}"""


# --------------------------------------------------------------------------- #
# Prompt do usuário
# --------------------------------------------------------------------------- #
def _montar_user_prompt(anomalias: list[dict]) -> str:
    itens = [
        {
            "ref": i,
            "campanha_id": a["campanha_id"],
            "campanha_nome": a["campanha_nome"],
            "objetivo": a["objetivo"],
            "metrica": a["metrica"],
            "tipo": a["tipo"],
            "severidade": a["severidade"],
            # 'resumo_do_fato' tem números só para seu ENTENDIMENTO; não copie.
            "resumo_do_fato": a["descricao"],
        }
        for i, a in enumerate(anomalias)
    ]
    campanhas = sorted({a["campanha_id"] for a in anomalias})
    return (
        "Anomalias detectadas (entrada determinística). Os números em "
        "'resumo_do_fato' servem apenas para o seu entendimento — NÃO os copie "
        "para o seu texto.\n\n"
        f"{json.dumps(itens, ensure_ascii=False, indent=2)}\n\n"
        f"Campanhas válidas: {campanhas}\n\n"
        "Gere o JSON pedido cobrindo TODAS as anomalias (uma entrada em "
        "'diagnosticos' por anomalia, usando o 'ref' correspondente) e UMA "
        "'narrativa' por campanha que tenha anomalias."
    )


# --------------------------------------------------------------------------- #
# Chamada ao Claude com validação + retry
# --------------------------------------------------------------------------- #
def _extrair_json(texto: str) -> str:
    """Extrai o objeto JSON da resposta (tolera cerca de markdown)."""
    t = texto.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = re.sub(r"^json\s*", "", t, flags=re.IGNORECASE).strip()
    inicio, fim = t.find("{"), t.rfind("}")
    if inicio == -1 or fim == -1 or fim < inicio:
        raise ValueError("nenhum objeto JSON encontrado na resposta do LLM")
    return t[inicio : fim + 1]


def _uma_chamada(client, settings: Settings, system: str, user: str) -> DiagnosticoLLM:
    """Uma tentativa: chama a API, extrai e valida o JSON (levanta em erro)."""
    resposta = client.messages.create(
        model=settings.llm_model_diagnostico,
        max_tokens=3000,
        temperature=settings.llm_temperature,  # temperatura baixa (config)
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    texto = "".join(
        bloco.text for bloco in resposta.content
        if getattr(bloco, "type", None) == "text"
    )
    if not texto.strip():
        raise ValueError("resposta vazia do LLM")
    dados = json.loads(_extrair_json(texto))  # JSONDecodeError -> retry
    return DiagnosticoLLM.model_validate(dados)  # ValidationError -> retry


def _chamar_com_retry(client, settings: Settings, system: str, user: str) -> DiagnosticoLLM:
    """Roda `_uma_chamada` com retry/backoff (tenacity)."""
    tentativas = max(1, settings.llm_max_retries)
    retryer = Retrying(
        stop=stop_after_attempt(tentativas),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(_ERROS_RETRY),
        reraise=True,
    )
    return retryer(_uma_chamada, client, settings, system, user)


# --------------------------------------------------------------------------- #
# Validações pós-resposta (anti-alucinação + auditoria de números)
# --------------------------------------------------------------------------- #
def _filtrar_alucinacoes(resposta: DiagnosticoLLM, anomalias: list[dict]):
    """Descarta diagnósticos/narrativas que citem campanha ou métrica inexistente."""
    pares_validos = {(a["campanha_id"], a["metrica"]) for a in anomalias}
    campanhas_validas = {a["campanha_id"] for a in anomalias}

    diagnosticos_ok, descartados = [], []
    for d in resposta.diagnosticos:
        if (d.campanha_id, d.metrica) not in pares_validos:
            motivo = f"campanha/métrica inexistente na entrada: {d.campanha_id}/{d.metrica}"
            descartados.append(
                {"campanha_id": d.campanha_id, "metrica": d.metrica, "ref": d.ref, "motivo": motivo}
            )
            log.warning("Diagnóstico DESCARTADO — %s", motivo)
        else:
            diagnosticos_ok.append(d)

    narrativas_ok = []
    for n in resposta.narrativas:
        if n.campanha_id not in campanhas_validas:
            descartados.append(
                {"campanha_id": n.campanha_id, "motivo": "narrativa de campanha inexistente"}
            )
            log.warning("Narrativa DESCARTADA — campanha inexistente: %s", n.campanha_id)
        else:
            narrativas_ok.append(n)

    return diagnosticos_ok, narrativas_ok, descartados


def _auditar_numeros(resumo: str, narrativas, diagnosticos, identificadores: set[str]) -> None:
    """Loga aviso se sobrar número no texto APÓS remover identificadores.

    IDs (ex.: "CAMP-003") e palavras do nome (ex.: "B2B") contêm dígitos
    legítimos; removê-los antes evita falso-positivo. O objetivo é flagrar
    número INVENTADO pelo LLM, não nome/ID citado.
    """
    ignorar = sorted((i for i in identificadores if i), key=len, reverse=True)

    def tem_numero(texto: str) -> bool:
        t = texto or ""
        for ident in ignorar:
            t = re.sub(re.escape(ident), " ", t, flags=re.IGNORECASE)
        return bool(re.search(r"\d", t))

    suspeitos = []
    if tem_numero(resumo):
        suspeitos.append("resumo_executivo")
    for n in narrativas:
        if tem_numero(n.narrativa):
            suspeitos.append(f"narrativa[{n.campanha_id}]")
    for d in diagnosticos:
        if tem_numero((d.diagnostico or "") + " " + (d.justificativa_acao or "")):
            suspeitos.append(f"diagnostico[{d.campanha_id}/{d.metrica}]")
    if suspeitos:
        log.warning(
            "LLM incluiu número(s) em: %s. Os números do relatório vêm da Fase 2; "
            "verifique se não houve invenção.",
            ", ".join(suspeitos),
        )


# --------------------------------------------------------------------------- #
# Montagem do relatório (números SEMPRE da Fase 2) e fallback degradado
# --------------------------------------------------------------------------- #
def _resumo_deterministico(saida_fase2: dict) -> str:
    por_sev = saida_fase2.get("anomalias_por_severidade", {})
    return (
        f"{saida_fase2.get('total_anomalias', 0)} anomalia(s) detectada(s) "
        f"({por_sev.get('crítica', 0)} crítica(s), {por_sev.get('alta', 0)} alta(s), "
        f"{por_sev.get('média', 0)} média(s)) em {saida_fase2.get('total_campanhas', 0)} "
        "campanha(s), na janela analisada."
    )


def _montar_relatorio(
    saida_fase2: dict,
    resumo_executivo: str,
    narrativas_ok: list,
    diagnosticos_ok: list,
    descartados: list,
    *,
    modelo: Optional[str],
    llm_disponivel: bool,
    aviso: Optional[str],
) -> dict:
    """Funde texto do LLM com os números (autoritativos) da Fase 2."""
    diag_por_par = {(d.campanha_id, d.metrica): d for d in diagnosticos_ok}
    narrativa_por_campanha = {n.campanha_id: n.narrativa for n in narrativas_ok}

    campanhas: dict[str, dict] = {}
    for a in saida_fase2.get("anomalias", []):
        cid = a["campanha_id"]
        if cid not in campanhas:
            campanhas[cid] = {
                "campanha_id": cid,
                "campanha_nome": a["campanha_nome"],
                "objetivo": a["objetivo"],
                "narrativa": narrativa_por_campanha.get(cid),
                "anomalias": [],
            }
        d = diag_por_par.get((cid, a["metrica"]))
        anomalia_saida = dict(a)  # preserva TODOS os números da Fase 2
        anomalia_saida["diagnostico"] = d.diagnostico if d else None
        anomalia_saida["acao_recomendada"] = d.acao.value if d else None
        anomalia_saida["justificativa_acao"] = d.justificativa_acao if d else None
        campanhas[cid]["anomalias"].append(anomalia_saida)

    return {
        "llm_disponivel": llm_disponivel,
        "aviso": aviso,
        "modelo": modelo,
        "janela_dias": saida_fase2.get("janela_dias"),
        "total_campanhas": saida_fase2.get("total_campanhas"),
        "total_anomalias": saida_fase2.get("total_anomalias"),
        "anomalias_por_severidade": saida_fase2.get("anomalias_por_severidade"),
        "resumo_executivo": resumo_executivo,
        "campanhas": list(campanhas.values()),
        "diagnosticos_descartados": descartados,
    }


def _degradado(saida_fase2: dict, motivo: str) -> dict:
    """Relatório degradado: só a parte determinística + aviso claro."""
    aviso = (
        f"Diagnóstico em linguagem natural INDISPONÍVEL ({motivo}). "
        "Relatório degradado: apenas a análise determinística da Fase 2."
    )
    log.warning(aviso)
    return _montar_relatorio(
        saida_fase2,
        _resumo_deterministico(saida_fase2),
        [], [], [],
        modelo=None,
        llm_disponivel=False,
        aviso=aviso,
    )


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def gerar_diagnostico(
    saida_fase2: dict,
    settings: Optional[Settings] = None,
    client=None,
) -> dict:
    """Gera o diagnóstico (LLM) a partir da saída da Fase 2. Nunca quebra.

    `client` permite injeção de um cliente fake nos testes (sem rede).
    """
    settings = settings or get_settings()
    anomalias = saida_fase2.get("anomalias", [])

    if not anomalias:
        log.info("Sem anomalias na entrada — diagnóstico não é necessário.")
        return _montar_relatorio(
            saida_fase2,
            "Nenhuma anomalia detectada na janela analisada.",
            [], [], [],
            modelo=None, llm_disponivel=True, aviso=None,
        )

    if client is None and not settings.anthropic_api_key:
        return _degradado(saida_fase2, "ANTHROPIC_API_KEY não configurada")

    system = SYSTEM_PROMPT
    user = _montar_user_prompt(anomalias)

    try:
        if client is None:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resposta = _chamar_com_retry(client, settings, system, user)
    except Exception as exc:  # noqa: BLE001 — fronteira de I/O: degradar, não quebrar
        log.error("Falha definitiva na camada LLM: %s: %s", type(exc).__name__, exc)
        return _degradado(
            saida_fase2,
            f"falha após {max(1, settings.llm_max_retries)} tentativa(s) "
            f"({type(exc).__name__})",
        )

    diagnosticos_ok, narrativas_ok, descartados = _filtrar_alucinacoes(resposta, anomalias)
    identificadores: set[str] = set()
    for a in anomalias:
        identificadores.add(a["campanha_id"])
        identificadores.update(a["campanha_nome"].split())
    _auditar_numeros(resposta.resumo_executivo, narrativas_ok, diagnosticos_ok, identificadores)
    log.info(
        "LLM OK | modelo=%s | diagnósticos: %d aceitos / %d descartados | narrativas: %d",
        settings.llm_model_diagnostico, len(diagnosticos_ok), len(descartados), len(narrativas_ok),
    )
    return _montar_relatorio(
        saida_fase2,
        resposta.resumo_executivo,
        narrativas_ok,
        diagnosticos_ok,
        descartados,
        modelo=settings.llm_model_diagnostico,
        llm_disponivel=True,
        aviso=None,
    )


# --------------------------------------------------------------------------- #
# Demo isolada da fase
# --------------------------------------------------------------------------- #
def _carregar_fase2(settings: Settings) -> dict:
    """Carrega output/anomalias.json; se não existir, gera via Fase 2."""
    caminho = settings.output_dir / "anomalias.json"
    if caminho.exists():
        log.info("Lendo entrada da Fase 2: %s", caminho)
        return json.loads(caminho.read_text(encoding="utf-8"))

    log.warning("%s não encontrado — gerando via Fase 2...", caminho)
    from src.analysis import analisar, construir_saida
    from src.ingestion import ingerir

    resultado = ingerir(settings.input_csv)
    anomalias = analisar(resultado.linhas)
    total = len({r.campanha_id for r in resultado.linhas})
    saida = construir_saida(anomalias, total)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(saida, ensure_ascii=False, indent=2), encoding="utf-8")
    return saida


def _demo() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    saida_fase2 = _carregar_fase2(settings)
    relatorio = gerar_diagnostico(saida_fase2, settings)

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    destino = settings.output_dir / "diagnostico.json"
    destino.write_text(
        json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "Fase 3 concluída | llm_disponivel=%s | JSON: %s",
        relatorio["llm_disponivel"], destino,
    )

    print(json.dumps(relatorio, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _demo()
