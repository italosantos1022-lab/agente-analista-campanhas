"""Testes da análise determinística (Fase 2) sobre a base real.

Validam que a régua certa é aplicada por objetivo e que as anomalias plantadas
no dataset são detectadas com a severidade esperada. Roda: uv run pytest
"""

from __future__ import annotations

from src.analysis import analisar
from src.config import get_settings
from src.ingestion import ingerir
from src.models import Objetivo, Severidade, TipoAnomalia


def _anomalias():
    res = ingerir(get_settings().input_csv)
    return analisar(res.linhas)


def _da(anoms, cid, tipo):
    return [a for a in anoms if a.campanha_id == cid and a.tipo == tipo]


def test_camp002_roas_em_queda_critica():
    # Remarketing (Vendas): ROAS 18.12 -> 0.74. Colapso de retorno.
    achadas = _da(_anomalias(), "CAMP-002", TipoAnomalia.ROAS_EM_QUEDA)
    assert achadas, "esperava roas_em_queda em CAMP-002"
    assert achadas[0].severidade == Severidade.CRITICA


def test_camp002_custo_por_compra_acima_baseline():
    achadas = _da(_anomalias(), "CAMP-002", TipoAnomalia.CUSTO_POR_COMPRA_ALTO)
    assert achadas and achadas[0].severidade == Severidade.CRITICA


def test_camp003_volume_leads_colapso_a_zero():
    # Captação Leads: 9 -> 0 leads. Colapso (crítica) por zerar.
    achadas = _da(_anomalias(), "CAMP-003", TipoAnomalia.VOLUME_LEADS_COLAPSO)
    assert achadas and achadas[0].severidade == Severidade.CRITICA
    assert achadas[0].evidencia["ultimo_dia"]["valor"] == 0


def test_camp003_custo_por_lead_disparando():
    achadas = _da(_anomalias(), "CAMP-003", TipoAnomalia.CUSTO_POR_LEAD_ALTO)
    assert achadas, "esperava custo_por_lead_alto em CAMP-003"


def test_camp006_conversas_colapso_e_custo_subindo():
    anoms = _anomalias()
    assert _da(anoms, "CAMP-006", TipoAnomalia.VOLUME_CONVERSAS_COLAPSO)
    assert _da(anoms, "CAMP-006", TipoAnomalia.CUSTO_POR_CONVERSA_ALTO)


def test_camp005_reconhecimento_fadiga_e_alcance():
    anoms = _anomalias()
    assert _da(anoms, "CAMP-005", TipoAnomalia.FREQUENCIA_ALTA)
    assert _da(anoms, "CAMP-005", TipoAnomalia.ALCANCE_COLAPSO)


def test_reconhecimento_nao_usa_roas():
    # Regra do CLAUDE.md: Reconhecimento NÃO deve gerar anomalia de ROAS.
    anoms = _anomalias()
    roas_recon = [
        a for a in anoms
        if a.objetivo == Objetivo.RECONHECIMENTO and a.tipo == TipoAnomalia.ROAS_EM_QUEDA
    ]
    assert roas_recon == []


def test_camp001_vendas_saudavel_sem_anomalias():
    # CAMP-001 é estável (após corrigir o clique negativo): nada deve disparar.
    anoms = [a for a in _anomalias() if a.campanha_id == "CAMP-001"]
    assert anoms == []
