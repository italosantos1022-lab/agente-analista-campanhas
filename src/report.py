"""Fase 4 — Relatório (JSON fonte da verdade + HTML via Jinja2).

Entradas:
  - output/anomalias.json (Fase 2): NÚMEROS (evidência) — fonte autoritativa.
  - output/diagnostico.json (Fase 3): TEXTO (resumo, narrativa, diagnóstico, ação).
  - ingestão (Fase 1): universo de campanhas (para listar as estáveis) e
    qualidade do dado (dias incompletos). Esses dados não existem nos dois JSONs,
    então o relatório os obtém da ingestão para ficar COMPLETO (não só alarme).

REGRA DE OURO: todo número do relatório vem do código (Fases 1-2). Do LLM só
entram campos de TEXTO (resumo_executivo, narrativa, diagnóstico, justificativa)
e a ação escolhida — nunca um número.

Execução: uv run python -m src.report
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import Settings, get_settings
from src.ingestion import IngestionResult, ingerir
from src.logging_config import setup_logging

log = logging.getLogger("report")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Ordem de severidade (pt-BR) e classe CSS correspondente.
_ORDEM_SEV = {"crítica": 0, "alta": 1, "média": 2}
SEV_CLASSE = {"crítica": "critica", "alta": "alta", "média": "media"}

# Rótulos legíveis para a UI (o JSON guarda os valores canônicos).
METRICA_LABEL = {
    "roas": "ROAS",
    "custo_por_compra": "Custo por compra",
    "ctr_link": "CTR de link",
    "frequencia": "Frequência",
    "custo_por_lead": "Custo por lead",
    "leads": "Volume de leads",
    "custo_por_conversa": "Custo por conversa",
    "conversas_iniciadas": "Volume de conversas",
    "alcance": "Alcance",
    "cpm": "CPM",
}
ACAO_LABEL = {
    "pausar_criativo": "Pausar criativo",
    "renovar_criativo": "Renovar criativo",
    "expandir_publico": "Expandir público",
    "trocar_publico": "Trocar público",
    "revisar_oferta": "Revisar oferta",
    "investigar_formulario": "Investigar formulário",
    "investigar_integracao": "Investigar integração",
    "realocar_verba": "Realocar verba",
}


# --------------------------------------------------------------------------- #
# Formatação pt-BR (só para exibição no HTML; o JSON mantém valores crus)
# --------------------------------------------------------------------------- #
def _fmt_num(valor) -> str:
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, int):
        return f"{valor:,}".replace(",", ".")
    if isinstance(valor, float):
        s = f"{valor:,.2f}"  # 1,272.60 (estilo en) -> trocar separadores
        return s.replace(",", "§").replace(".", ",").replace("§", ".")
    return str(valor)


def _fmt_pct(valor) -> str:
    return f"{valor:+.1f}".replace(".", ",") + "%"


def _data_br(iso: str) -> str:
    try:
        ano, mes, dia = iso.split("-")
        return f"{dia}/{mes}/{ano}"
    except (ValueError, AttributeError):
        return iso


def _descricao_br(texto: str) -> str:
    """Converte os números embutidos na descrição para pt-BR (vírgula decimal,
    ponto de milhar), igual à lista de evidência. Ex.: '95.9% (54444 -> 0.74)'
    -> '95,9% (54.444 -> 0,74)'. Seguro: a descrição não contém datas nem IDs.
    """
    def _repl(m: re.Match) -> str:
        token = m.group(0)
        if "." in token:
            inteiro, decimal = token.split(".")
            return f"{int(inteiro):,}".replace(",", ".") + "," + decimal
        return f"{int(token):,}".replace(",", ".")

    return re.sub(r"\d+(?:\.\d+)?", _repl, texto)


def _evidencia_linhas(ev: dict) -> list[tuple[str, str]]:
    """Transforma a evidência (números do código) em pares (rótulo, valor)."""
    linhas: list[tuple[str, str]] = []
    if "primeiro_dia" in ev:
        p = ev["primeiro_dia"]
        linhas.append(("Primeiro dia", f'{_data_br(p["data"])}: {_fmt_num(p["valor"])}'))
    if "ultimo_dia" in ev:
        u = ev["ultimo_dia"]
        linhas.append(("Último dia", f'{_data_br(u["data"])}: {_fmt_num(u["valor"])}'))
    if ev.get("variacao_pct") is not None:
        linhas.append(("Variação no período", _fmt_pct(ev["variacao_pct"])))
    if "baseline_media" in ev:
        linhas.append(("Baseline (média da campanha)", _fmt_num(ev["baseline_media"])))
    if "desvio_pct" in ev:
        linhas.append(("Desvio vs baseline", _fmt_pct(ev["desvio_pct"])))
    if "limiar_critico" in ev:
        linhas.append(("Limiar crítico", _fmt_num(ev["limiar_critico"])))
    if "janela_dias" in ev:
        linhas.append(("Dias considerados", str(ev["janela_dias"])))
    return linhas


# --------------------------------------------------------------------------- #
# Construção do relatório (fonte da verdade)
# --------------------------------------------------------------------------- #
def construir_relatorio(
    anomalias_fase2: dict, diagnostico_fase3: dict, ingestao: IngestionResult
) -> dict:
    """Funde números (Fase 2) + texto (Fase 3) + universo/qualidade (Fase 1)."""
    anomalias = anomalias_fase2.get("anomalias", [])

    # Texto do LLM, indexado para anexar aos números (sem trazer números do LLM).
    texto_por_anomalia: dict[tuple, dict] = {}
    narrativa_por_campanha: dict[str, Optional[str]] = {}
    for c in diagnostico_fase3.get("campanhas", []):
        narrativa_por_campanha[c["campanha_id"]] = c.get("narrativa")
        for a in c.get("anomalias", []):
            texto_por_anomalia[(a["campanha_id"], a["metrica"])] = {
                "diagnostico": a.get("diagnostico"),
                "acao_recomendada": a.get("acao_recomendada"),
                "justificativa_acao": a.get("justificativa_acao"),
            }

    # Universo de campanhas e qualidade do dado, da ingestão (Fase 1).
    universo: dict[str, dict] = {}
    datas = []
    incompletas: list[dict] = []
    for r in ingestao.linhas:
        datas.append(r.data)
        info = universo.setdefault(
            r.campanha_id,
            {"campanha_nome": r.campanha_nome, "objetivo": r.objetivo.value},
        )
        if r.incompleto:
            incompletas.append(
                {
                    "campanha_id": r.campanha_id,
                    "data": r.data.isoformat(),
                    "campos_faltando": r.campos_faltando,
                }
            )

    # Anomalias por campanha: NÚMEROS da Fase 2 + TEXTO da Fase 3.
    anomalias_por_campanha: dict[str, list] = {}
    for a in anomalias:
        cid = a["campanha_id"]
        texto = texto_por_anomalia.get((cid, a["metrica"]), {})
        anomalias_por_campanha.setdefault(cid, []).append(
            {
                "metrica": a["metrica"],
                "tipo": a["tipo"],
                "severidade": a["severidade"],
                "descricao": _descricao_br(a["descricao"]),  # números em pt-BR
                "evidencia": a["evidencia"],  # números do código (Fase 2)
                "diagnostico": texto.get("diagnostico"),
                "acao_recomendada": texto.get("acao_recomendada"),
                "justificativa_acao": texto.get("justificativa_acao"),
            }
        )

    # Lista de campanhas (com anomalias + estáveis).
    campanhas = []
    for cid, info in universo.items():
        anoms = sorted(
            anomalias_por_campanha.get(cid, []),
            key=lambda x: _ORDEM_SEV[x["severidade"]],
        )
        campanhas.append(
            {
                "campanha_id": cid,
                "campanha_nome": info["campanha_nome"],
                "objetivo": info["objetivo"],
                "status": "anomalias" if anoms else "estavel",
                "narrativa": narrativa_por_campanha.get(cid),
                "anomalias": anoms,
            }
        )

    # Ordena: campanhas com anomalia (pior severidade primeiro), depois estáveis.
    def _rank(c):
        estavel = c["status"] == "estavel"
        pior = 99 if estavel else min(_ORDEM_SEV[a["severidade"]] for a in c["anomalias"])
        return (1 if estavel else 0, pior, c["campanha_id"])

    campanhas.sort(key=_rank)

    totais = {
        "campanhas": len(universo),
        "anomalias": len(anomalias),
        "por_severidade": anomalias_fase2.get("anomalias_por_severidade", {}),
        "campanhas_com_anomalia": sum(1 for c in campanhas if c["status"] == "anomalias"),
        "campanhas_estaveis": sum(1 for c in campanhas if c["status"] == "estavel"),
    }

    return {
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "periodo": {
            "inicio": min(datas).isoformat() if datas else None,
            "fim": max(datas).isoformat() if datas else None,
        },
        "janela_dias": anomalias_fase2.get("janela_dias"),
        "llm_disponivel": diagnostico_fase3.get("llm_disponivel", False),
        "aviso_llm": diagnostico_fase3.get("aviso"),
        "modelo_llm": diagnostico_fase3.get("modelo"),
        "resumo_executivo": diagnostico_fase3.get("resumo_executivo", ""),
        "totais": totais,
        "campanhas": campanhas,
        "qualidade_dado": {
            "limpezas": {
                "duplicatas_removidas": ingestao.resumo.get("duplicatas_removidas", 0),
                "gasto_formato_br_corrigido": ingestao.resumo.get("gasto_formato_br_corrigido", 0),
                "cliques_link_negativos_corrigidos": ingestao.resumo.get(
                    "cliques_link_negativos_corrigidos", 0
                ),
            },
            "linhas_incompletas": incompletas,
        },
    }


# --------------------------------------------------------------------------- #
# Renderização HTML (Jinja2)
# --------------------------------------------------------------------------- #
def _ambiente_jinja() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),  # escapa texto do LLM
    )
    env.filters["classe_sev"] = lambda s: SEV_CLASSE.get(s, "media")
    env.filters["metrica_label"] = lambda m: METRICA_LABEL.get(m, m)
    env.filters["acao_label"] = lambda a: ACAO_LABEL.get(a, a) if a else ""
    env.filters["evidencia_linhas"] = _evidencia_linhas
    env.filters["data_br"] = _data_br
    return env


def renderizar_html(relatorio: dict) -> str:
    template = _ambiente_jinja().get_template("report.html.j2")
    return template.render(**relatorio)


# --------------------------------------------------------------------------- #
# Orquestração / demo
# --------------------------------------------------------------------------- #
def gerar(settings: Optional[Settings] = None) -> dict:
    """Lê as entradas, monta o JSON e o HTML, grava os dois arquivos."""
    settings = settings or get_settings()
    saida = settings.output_dir
    saida.mkdir(parents=True, exist_ok=True)

    ingestao = ingerir(settings.input_csv)

    caminho_anom = saida / "anomalias.json"
    if not caminho_anom.exists():
        log.warning("%s ausente — gerando via Fase 2.", caminho_anom)
        from src.analysis import analisar, construir_saida

        anomalias_fase2 = construir_saida(
            analisar(ingestao.linhas), len({r.campanha_id for r in ingestao.linhas})
        )
    else:
        anomalias_fase2 = json.loads(caminho_anom.read_text(encoding="utf-8"))

    caminho_diag = saida / "diagnostico.json"
    if not caminho_diag.exists():
        raise FileNotFoundError(
            f"{caminho_diag} não encontrado. Rode a Fase 3 primeiro: "
            "uv run python -m src.llm"
        )
    diagnostico_fase3 = json.loads(caminho_diag.read_text(encoding="utf-8"))

    relatorio = construir_relatorio(anomalias_fase2, diagnostico_fase3, ingestao)

    (saida / "relatorio.json").write_text(
        json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (saida / "relatorio.html").write_text(renderizar_html(relatorio), encoding="utf-8")

    log.info(
        "Fase 4 concluída | %d campanha(s): %d com anomalia, %d estável(is) | "
        "JSON: %s | HTML: %s",
        relatorio["totais"]["campanhas"],
        relatorio["totais"]["campanhas_com_anomalia"],
        relatorio["totais"]["campanhas_estaveis"],
        saida / "relatorio.json",
        saida / "relatorio.html",
    )
    return relatorio


def _demo() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    relatorio = gerar(settings)
    print(json.dumps(relatorio, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _demo()
