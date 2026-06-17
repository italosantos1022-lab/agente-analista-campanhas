"""Análise determinística por objetivo (Fase 2). SEM IA.

Regra de Ouro (CLAUDE.md): TODA a matemática acontece aqui — métricas,
tendência, baseline e detecção de anomalia por regra. Nenhuma chamada a LLM.

Para cada campanha:
  - agrupa as linhas e ordena por data;
  - baseline = média da PRÓPRIA campanha na janela (7 dias);
  - tendência = primeiro vs último dia + variação percentual;
  - detecta anomalias com a métrica certa para o objetivo;
  - atribui severidade (crítica/alta/média) por regra (thresholds comentados).

Saída: JSON estruturado (campanha, métrica, o que aconteceu, severidade e os
números que comprovam).

Execução isolada:
    uv run python -m src.analysis
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Optional

from src.config import get_settings
from src.ingestion import ingerir
from src.logging_config import setup_logging
from src.models import Anomalia, CampanhaRow, Objetivo, Severidade, TipoAnomalia

log = logging.getLogger("analysis")

# Janela de baseline: média da própria campanha nestes dias (CLAUDE.md).
JANELA_DIAS = 7

# --------------------------------------------------------------------------- #
# THRESHOLDS — cada faixa é (média, alta, crítica) e está comentada.
# A régua é por OBJETIVO. Os valores valem para a janela de 7 dias e foram
# calibrados para sair do RUÍDO diário típico de mídia paga sem gerar alarme
# falso (ex.: oscilação normal de ROAS/CTR de um dia para outro).
# --------------------------------------------------------------------------- #

# Queda de ROAS (Vendas) — variação % do 1º vs último dia.
# Por quê: ROAS tem ruído diário; <20% costuma ser flutuação. 20% pede atenção,
# 40% indica perda real de rentabilidade, 60%+ é colapso do retorno.
ROAS_QUEDA = (20.0, 40.0, 60.0)

# Custo por compra acima do baseline (Vendas) — desvio % do último dia vs média.
# Por quê: passar 25% acima da PRÓPRIA média sai do ruído; dobrar (100%) é crítico.
CPA_ACIMA = (25.0, 50.0, 100.0)

# Queda de CTR de link (Vendas, Leads, Reconhecimento) — variação % 1º vs último.
# Por quê: CTR é sensível a fadiga/relevância; 20% já sinaliza, 50% é colapso de
# engajamento (o público parou de clicar).
CTR_QUEDA = (20.0, 35.0, 50.0)

# Frequência alta = fadiga de criativo — valor ABSOLUTO do último dia.
# Por quê: numa janela de 7 dias, freq ~3 começa saturação, >=4 é fadiga clara,
# >=5 é desgaste severo (mesmo público vê demais -> CTR cai e CPM sobe). Aqui o
# threshold é absoluto (não %), porque frequência é uma contagem acumulada.
FREQUENCIA_ALTA = (3.0, 4.0, 5.0)

# Custo por lead disparando (Leads) — desvio % do último valor vs baseline.
# Por quê: custo por lead é volátil; 30% acima da média sinaliza, dobrar+ é crítico.
CPL_ACIMA = (30.0, 60.0, 120.0)

# Colapso de volume de leads (Leads) — queda % 1º vs último. Zerar => crítica.
# Por quê: -30%/dia já é relevante, -80% é quase parada; 0 lead é colapso total
# (e a divisão para custo por lead deixa de existir).
VOLUME_LEADS_QUEDA = (30.0, 50.0, 80.0)

# Custo por conversa subindo (Mensagens) — desvio % do último valor vs baseline.
CPCONV_ACIMA = (30.0, 60.0, 120.0)

# Colapso de conversas (Mensagens) — queda % 1º vs último. Zerar => crítica.
VOLUME_CONVERSAS_QUEDA = (30.0, 50.0, 80.0)

# Colapso de alcance (Reconhecimento) — queda % 1º vs último.
# Por quê: alcance caindo = o criativo parou de atingir gente nova (saturação do
# público); >60% é colapso de distribuição. Para Reconhecimento NÃO se usa ROAS.
ALCANCE_QUEDA = (25.0, 40.0, 60.0)

# CPM acima do baseline (Reconhecimento) — desvio % do último dia vs média.
# Por quê: CPM subindo encarece a entrega; comparar à própria média da campanha.
CPM_ACIMA = (25.0, 50.0, 100.0)

# Ordem de severidade para ordenar a saída (mais grave primeiro).
_ORDEM_SEV = {Severidade.CRITICA: 0, Severidade.ALTA: 1, Severidade.MEDIA: 2}


# --------------------------------------------------------------------------- #
# Helpers de cálculo
# --------------------------------------------------------------------------- #
def _severidade(valor: float, faixas: tuple[float, float, float]) -> Optional[Severidade]:
    """Mapeia um valor (queda %, desvio % ou frequência absoluta) -> severidade."""
    media, alta, critica = faixas
    if valor >= critica:
        return Severidade.CRITICA
    if valor >= alta:
        return Severidade.ALTA
    if valor >= media:
        return Severidade.MEDIA
    return None


def _serie(rows: list[CampanhaRow], attr: str) -> list[tuple[date, float]]:
    """Série (data, valor) de uma métrica, ignorando dias sem o dado."""
    return [(r.data, getattr(r, attr)) for r in rows if getattr(r, attr) is not None]


def _variacao_pct(primeiro: float, ultimo: float) -> Optional[float]:
    """Variação percentual do primeiro para o último valor (None se base 0)."""
    if primeiro == 0:
        return None
    return (ultimo - primeiro) / primeiro * 100.0


# --------------------------------------------------------------------------- #
# Análise de uma campanha
# --------------------------------------------------------------------------- #
def _analisar_campanha(rows: list[CampanhaRow]) -> list[Anomalia]:
    rows = sorted(rows, key=lambda r: r.data)
    objetivo = rows[0].objetivo
    cid = rows[0].campanha_id
    nome = rows[0].campanha_nome
    anomalias: list[Anomalia] = []

    def add(tipo, metrica, descricao, sev, evidencia):
        anomalias.append(
            Anomalia(
                campanha_id=cid,
                campanha_nome=nome,
                objetivo=objetivo,
                metrica=metrica,
                tipo=tipo,
                descricao=descricao,
                severidade=sev,
                evidencia=evidencia,
            )
        )

    def check_queda(attr, tipo, faixas, rotulo, zero_critico=False):
        """Tendência de queda: primeiro vs último dia da série."""
        serie = _serie(rows, attr)
        if len(serie) < 2:
            return
        (d0, v0), (dn, vn) = serie[0], serie[-1]
        var = _variacao_pct(v0, vn)
        if var is None:
            return
        queda = -var  # positivo quando caiu
        sev = _severidade(queda, faixas)
        if zero_critico and vn == 0:
            sev = Severidade.CRITICA  # zerar volume é sempre colapso
        if sev is None:
            return
        base = mean(v for _, v in serie)
        if vn == 0:
            descricao = f"{rotulo} despencou a zero no período ({v0:g} -> 0)."
        else:
            descricao = f"{rotulo} caiu {queda:.1f}% no período ({v0:g} -> {vn:g})."
        add(
            tipo,
            attr,
            descricao,
            sev,
            {
                "primeiro_dia": {"data": d0.isoformat(), "valor": v0},
                "ultimo_dia": {"data": dn.isoformat(), "valor": vn},
                "variacao_pct": round(var, 1),
                "baseline_media": round(base, 2),
                "janela_dias": len(serie),
            },
        )

    def check_acima_baseline(attr, tipo, faixas, rotulo):
        """Custo do último dia acima da média (baseline) da própria campanha."""
        serie = _serie(rows, attr)
        if len(serie) < 2:
            return
        base = mean(v for _, v in serie)
        if base == 0:
            return
        dn, vn = serie[-1]
        desvio = (vn - base) / base * 100.0
        sev = _severidade(desvio, faixas)
        if sev is None:
            return
        add(
            tipo,
            attr,
            f"{rotulo} do último dia ({vn:g}) está {desvio:.1f}% acima do "
            f"baseline da campanha ({base:.2f}).",
            sev,
            {
                "ultimo_dia": {"data": dn.isoformat(), "valor": vn},
                "baseline_media": round(base, 2),
                "desvio_pct": round(desvio, 1),
                "janela_dias": len(serie),
            },
        )

    def check_frequencia():
        """Fadiga de criativo: frequência absoluta do último dia."""
        serie = _serie(rows, "frequencia")
        if not serie:
            return
        (d0, v0), (dn, vn) = serie[0], serie[-1]
        sev = _severidade(vn, FREQUENCIA_ALTA)
        if sev is None:
            return
        var = _variacao_pct(v0, vn)
        add(
            TipoAnomalia.FREQUENCIA_ALTA,
            "frequencia",
            f"Frequência atingiu {vn:g} no último dia (fadiga de criativo; "
            f"era {v0:g} no início).",
            sev,
            {
                "primeiro_dia": {"data": d0.isoformat(), "valor": v0},
                "ultimo_dia": {"data": dn.isoformat(), "valor": vn},
                "variacao_pct": round(var, 1) if var is not None else None,
                "limiar_critico": FREQUENCIA_ALTA[2],
            },
        )

    # --- Régua por objetivo (métrica certa para cada um) --------------------
    if objetivo == Objetivo.VENDAS:
        check_queda("roas", TipoAnomalia.ROAS_EM_QUEDA, ROAS_QUEDA, "ROAS")
        check_acima_baseline(
            "custo_por_compra", TipoAnomalia.CUSTO_POR_COMPRA_ALTO, CPA_ACIMA,
            "Custo por compra",
        )
        check_queda("ctr_link", TipoAnomalia.CTR_EM_QUEDA, CTR_QUEDA, "CTR de link")
        check_frequencia()

    elif objetivo == Objetivo.LEADS:
        check_acima_baseline(
            "custo_por_lead", TipoAnomalia.CUSTO_POR_LEAD_ALTO, CPL_ACIMA,
            "Custo por lead",
        )
        check_queda(
            "leads", TipoAnomalia.VOLUME_LEADS_COLAPSO, VOLUME_LEADS_QUEDA,
            "Volume de leads", zero_critico=True,
        )
        check_queda("ctr_link", TipoAnomalia.CTR_EM_QUEDA, CTR_QUEDA, "CTR de link")

    elif objetivo == Objetivo.MENSAGENS:
        check_acima_baseline(
            "custo_por_conversa", TipoAnomalia.CUSTO_POR_CONVERSA_ALTO, CPCONV_ACIMA,
            "Custo por conversa",
        )
        check_queda(
            "conversas_iniciadas", TipoAnomalia.VOLUME_CONVERSAS_COLAPSO,
            VOLUME_CONVERSAS_QUEDA, "Volume de conversas", zero_critico=True,
        )

    elif objetivo == Objetivo.RECONHECIMENTO:
        # Reconhecimento NÃO usa ROAS (CLAUDE.md): foco em fadiga e distribuição.
        check_frequencia()
        check_queda(
            "alcance", TipoAnomalia.ALCANCE_COLAPSO, ALCANCE_QUEDA,
            "Alcance", zero_critico=True,
        )
        check_acima_baseline("cpm", TipoAnomalia.CPM_ALTO, CPM_ACIMA, "CPM")
        check_queda("ctr_link", TipoAnomalia.CTR_EM_QUEDA, CTR_QUEDA, "CTR de link")

    return anomalias


# --------------------------------------------------------------------------- #
# Orquestração da análise
# --------------------------------------------------------------------------- #
def analisar(linhas: list[CampanhaRow]) -> list[Anomalia]:
    """Agrupa por campanha e roda a detecção determinística em cada uma."""
    grupos: dict[str, list[CampanhaRow]] = defaultdict(list)
    for linha in linhas:
        grupos[linha.campanha_id].append(linha)

    anomalias: list[Anomalia] = []
    for cid, rows in grupos.items():
        achadas = _analisar_campanha(rows)
        log.info(
            "Campanha %s (%s): %d dia(s), %d anomalia(s).",
            cid, rows[0].objetivo.value, len(rows), len(achadas),
        )
        anomalias.extend(achadas)

    # Mais grave primeiro; depois por campanha e métrica (saída estável).
    anomalias.sort(key=lambda a: (_ORDEM_SEV[a.severidade], a.campanha_id, a.metrica))
    return anomalias


def construir_saida(anomalias: list[Anomalia], total_campanhas: int) -> dict:
    """Monta o dicionário JSON estruturado da Fase 2."""
    por_sev = {s.value: 0 for s in Severidade}
    for a in anomalias:
        por_sev[a.severidade.value] += 1
    return {
        "janela_dias": JANELA_DIAS,
        "total_campanhas": total_campanhas,
        "total_anomalias": len(anomalias),
        "anomalias_por_severidade": por_sev,
        "anomalias": [a.model_dump(mode="json") for a in anomalias],
    }


# --------------------------------------------------------------------------- #
# Demo isolada da fase
# --------------------------------------------------------------------------- #
def _demo() -> None:
    parser = argparse.ArgumentParser(
        description="Análise determinística por objetivo (Fase 2)."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Sobrescreve o CSV de entrada (default: config/INPUT_CSV).",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    resultado = ingerir(args.csv or settings.input_csv)
    anomalias = analisar(resultado.linhas)
    total_campanhas = len({r.campanha_id for r in resultado.linhas})
    saida = construir_saida(anomalias, total_campanhas)

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    destino = settings.output_dir / "anomalias.json"
    destino.write_text(
        json.dumps(saida, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "Análise concluída | %d anomalia(s): %s | JSON: %s",
        saida["total_anomalias"], saida["anomalias_por_severidade"], destino,
    )

    print(json.dumps(saida, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _demo()
