"""Testes do relatório (Fase 4).

Foco: (1) números vêm do código (Fase 2), nunca do LLM; (2) campanhas estáveis
listadas; (3) seção de qualidade cita o dia incompleto da CAMP-004; (4) o HTML
renderiza com badges de severidade.
"""

from __future__ import annotations

from src.analysis import analisar, construir_saida
from src.config import get_settings
from src.ingestion import ingerir
from src.report import construir_relatorio, renderizar_html


def _pipeline_ate_fase2():
    ingest = ingerir(get_settings().input_csv)
    saida2 = construir_saida(analisar(ingest.linhas), len({r.campanha_id for r in ingest.linhas}))
    return ingest, saida2


def _diag_fake(saida2):
    """Diagnóstico (Fase 3) FAKE com números ERRADOS de propósito, para provar
    que o relatório ignora números do LLM e usa os da Fase 2."""
    campanhas = {}
    for a in saida2["anomalias"]:
        c = campanhas.setdefault(a["campanha_id"], {"campanha_id": a["campanha_id"],
                                                    "narrativa": f"narrativa {a['campanha_id']}",
                                                    "anomalias": []})
        c["anomalias"].append({
            "campanha_id": a["campanha_id"], "metrica": a["metrica"],
            "diagnostico": "texto do LLM", "acao_recomendada": "realocar_verba",
            "justificativa_acao": "porque sim",
            "evidencia": {"variacao_pct": 123456.0},  # número ERRADO (do LLM) — deve ser ignorado
        })
    return {
        "llm_disponivel": True, "aviso": None, "modelo": "fake-model",
        "resumo_executivo": "Resumo do LLM.", "campanhas": list(campanhas.values()),
    }


def test_descricao_em_formato_br():
    from src.report import _descricao_br

    assert (
        _descricao_br("ROAS caiu 95.9% no período (18.12 -> 0.74).")
        == "ROAS caiu 95,9% no período (18,12 -> 0,74)."
    )
    assert (
        _descricao_br("Alcance caiu 63.6% no período (54444 -> 19808).")
        == "Alcance caiu 63,6% no período (54.444 -> 19.808)."
    )
    # Inteiros simples não ganham vírgula decimal.
    assert _descricao_br("(9 -> 0)") == "(9 -> 0)"


def test_descricao_no_relatorio_sai_em_br():
    ingest, saida2 = _pipeline_ate_fase2()
    rel = construir_relatorio(saida2, _diag_fake(saida2), ingest)
    roas = next(
        a for c in rel["campanhas"] if c["campanha_id"] == "CAMP-002"
        for a in c["anomalias"] if a["metrica"] == "roas"
    )
    assert "18,12" in roas["descricao"] and "0,74" in roas["descricao"]
    assert "18.12" not in roas["descricao"]


def test_numeros_vem_da_fase2_nao_do_llm():
    ingest, saida2 = _pipeline_ate_fase2()
    rel = construir_relatorio(saida2, _diag_fake(saida2), ingest)

    camp002 = next(c for c in rel["campanhas"] if c["campanha_id"] == "CAMP-002")
    roas = next(a for a in camp002["anomalias"] if a["metrica"] == "roas")
    # Número correto da Fase 2, não o 123456 injetado no diag fake.
    assert roas["evidencia"]["variacao_pct"] == -95.9
    # Texto veio do LLM.
    assert roas["diagnostico"] == "texto do LLM"
    assert roas["acao_recomendada"] == "realocar_verba"


def test_campanhas_estaveis_listadas():
    ingest, saida2 = _pipeline_ate_fase2()
    rel = construir_relatorio(saida2, _diag_fake(saida2), ingest)
    estaveis = {c["campanha_id"] for c in rel["campanhas"] if c["status"] == "estavel"}
    assert estaveis == {"CAMP-001", "CAMP-004"}
    assert rel["totais"]["campanhas_estaveis"] == 2
    assert rel["totais"]["campanhas"] == 6


def test_qualidade_dado_cita_camp004_incompleta():
    ingest, saida2 = _pipeline_ate_fase2()
    rel = construir_relatorio(saida2, _diag_fake(saida2), ingest)
    incompletas = rel["qualidade_dado"]["linhas_incompletas"]
    achou = [i for i in incompletas if i["campanha_id"] == "CAMP-004" and i["data"] == "2026-06-03"]
    assert achou, "esperava CAMP-004 2026-06-03 na qualidade de dado"
    assert "compras" in achou[0]["campos_faltando"]
    # limpezas reportadas
    assert rel["qualidade_dado"]["limpezas"]["duplicatas_removidas"] == 1


def test_ordenacao_criticas_primeiro_estaveis_por_ultimo():
    ingest, saida2 = _pipeline_ate_fase2()
    rel = construir_relatorio(saida2, _diag_fake(saida2), ingest)
    status = [c["status"] for c in rel["campanhas"]]
    # Todas as 'anomalias' antes de qualquer 'estavel'.
    assert status == sorted(status, key=lambda s: 0 if s == "anomalias" else 1)
    assert status[-1] == "estavel"


def test_html_renderiza_com_badges_e_secoes():
    ingest, saida2 = _pipeline_ate_fase2()
    rel = construir_relatorio(saida2, _diag_fake(saida2), ingest)
    html = renderizar_html(rel)

    assert "Resumo executivo" in html
    assert "Campanhas estáveis" in html
    assert "Qualidade do dado" in html
    assert "badge sev-critica" in html  # badge colorido de crítica
    assert "CAMP-001" in html and "CAMP-004" in html  # estáveis aparecem
    assert "03/06/2026" in html  # data do dia incompleto formatada pt-BR
    # Rótulo de ação humanizado (não o valor cru do enum).
    assert "Realocar verba" in html


def test_html_escapa_texto_do_llm():
    ingest, saida2 = _pipeline_ate_fase2()
    diag = _diag_fake(saida2)
    diag["resumo_executivo"] = "<script>alert(1)</script>"
    html = renderizar_html(construir_relatorio(saida2, diag, ingest))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
