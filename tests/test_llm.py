"""Testes da camada LLM (Fase 3) — SEM rede, com cliente fake injetado.

Cobre: merge determinístico (números da Fase 2 preservados), restrição de ações,
filtro anti-alucinação, retry do tenacity e fallback degradado.
"""

from __future__ import annotations

import json

import anthropic
import pytest

from src.config import Settings
from src.llm import gerar_diagnostico


# --- Infra de mock --------------------------------------------------------- #
class _Bloco:
    type = "text"

    def __init__(self, texto):
        self.text = texto


class _Resposta:
    def __init__(self, texto):
        self.content = [_Bloco(texto)]


class FakeMessages:
    def __init__(self, comportamento):
        self._comportamento = comportamento  # str | Exception | list desses
        self.chamadas = 0

    def create(self, **kwargs):
        self.chamadas += 1
        item = self._comportamento
        if isinstance(item, list):
            item = item[min(self.chamadas - 1, len(item) - 1)]
        if isinstance(item, Exception):
            raise item
        return _Resposta(item)


class FakeClient:
    def __init__(self, comportamento):
        self.messages = FakeMessages(comportamento)


def _settings(**kw):
    base = dict(anthropic_api_key="chave-fake", llm_max_retries=2, llm_temperature=0.0)
    base.update(kw)
    # _env_file=None: teste hermético, não lê o .env real da máquina.
    return Settings(_env_file=None, **base)


# Saída mínima de Fase 2 com duas anomalias em campanhas/objetivos diferentes.
SAIDA_FASE2 = {
    "janela_dias": 7,
    "total_campanhas": 2,
    "total_anomalias": 2,
    "anomalias_por_severidade": {"crítica": 1, "alta": 1, "média": 0},
    "anomalias": [
        {
            "campanha_id": "CAMP-002", "campanha_nome": "Remarketing", "objetivo": "Vendas",
            "metrica": "roas", "tipo": "roas_em_queda", "severidade": "crítica",
            "descricao": "ROAS caiu 95.9% (18.12 -> 0.74).", "evidencia": {"variacao_pct": -95.9},
        },
        {
            "campanha_id": "CAMP-003", "campanha_nome": "Leads B2B", "objetivo": "Leads",
            "metrica": "leads", "tipo": "volume_leads_colapso", "severidade": "alta",
            "descricao": "Leads despencaram a zero.", "evidencia": {"variacao_pct": -100.0},
        },
    ],
}


def _json_llm(diagnosticos, narrativas=None, resumo="Resumo crítico."):
    return json.dumps(
        {
            "resumo_executivo": resumo,
            "narrativas": narrativas or [{"campanha_id": "CAMP-002", "narrativa": "saturação"}],
            "diagnosticos": diagnosticos,
        },
        ensure_ascii=False,
    )


# --- Testes ---------------------------------------------------------------- #
def test_merge_preserva_numeros_e_texto():
    diags = [
        {"ref": 0, "campanha_id": "CAMP-002", "metrica": "roas",
         "diagnostico": "Retorno desabando.", "acao": "renovar_criativo",
         "justificativa_acao": "criativo saturado"},
        {"ref": 1, "campanha_id": "CAMP-003", "metrica": "leads",
         "diagnostico": "Captação parou.", "acao": "investigar_formulario",
         "justificativa_acao": "possível quebra de form"},
    ]
    client = FakeClient(_json_llm(diags))
    rel = gerar_diagnostico(SAIDA_FASE2, _settings(), client=client)

    assert rel["llm_disponivel"] is True
    assert rel["aviso"] is None
    # Números continuam vindo da Fase 2.
    anom = rel["campanhas"][0]["anomalias"][0]
    assert anom["evidencia"]["variacao_pct"] == -95.9
    # Texto e ação vieram do LLM.
    assert anom["diagnostico"] == "Retorno desabando."
    assert anom["acao_recomendada"] == "renovar_criativo"


def test_acao_fora_do_conjunto_e_rejeitada_e_degrada():
    diags = [{"ref": 0, "campanha_id": "CAMP-002", "metrica": "roas",
              "diagnostico": "x", "acao": "explodir_a_conta", "justificativa_acao": "y"}]
    client = FakeClient(_json_llm(diags))
    rel = gerar_diagnostico(SAIDA_FASE2, _settings(), client=client)
    # Ação inválida -> ValidationError -> retry esgota -> degradado.
    assert rel["llm_disponivel"] is False
    assert client.messages.chamadas == 2  # tentou llm_max_retries vezes


def test_descarta_campanha_inexistente():
    diags = [
        {"ref": 0, "campanha_id": "CAMP-002", "metrica": "roas",
         "diagnostico": "ok", "acao": "renovar_criativo", "justificativa_acao": "j"},
        {"ref": 99, "campanha_id": "CAMP-999", "metrica": "roas",
         "diagnostico": "alucinado", "acao": "pausar_criativo", "justificativa_acao": "j"},
    ]
    client = FakeClient(_json_llm(diags))
    rel = gerar_diagnostico(SAIDA_FASE2, _settings(), client=client)
    descartados = rel["diagnosticos_descartados"]
    assert any(d.get("campanha_id") == "CAMP-999" for d in descartados)
    # A anomalia real recebeu diagnóstico; a alucinada não entrou em lugar nenhum.
    todas = [a for c in rel["campanhas"] for a in c["anomalias"]]
    assert all(a["campanha_id"] != "CAMP-999" for a in todas)


def test_metrica_inexistente_e_descartada():
    diags = [{"ref": 0, "campanha_id": "CAMP-002", "metrica": "metrica_inventada",
              "diagnostico": "x", "acao": "realocar_verba", "justificativa_acao": "j"}]
    client = FakeClient(_json_llm(diags))
    rel = gerar_diagnostico(SAIDA_FASE2, _settings(), client=client)
    assert rel["diagnosticos_descartados"]
    # Nenhuma anomalia recebeu diagnóstico (a única citação era inválida).
    todas = [a for c in rel["campanhas"] for a in c["anomalias"]]
    assert all(a["diagnostico"] is None for a in todas)


def test_retry_recupera_de_json_invalido():
    diags = [{"ref": 0, "campanha_id": "CAMP-002", "metrica": "roas",
              "diagnostico": "ok", "acao": "renovar_criativo", "justificativa_acao": "j"}]
    # 1ª resposta: lixo (JSON inválido) -> retry; 2ª: válida.
    client = FakeClient(["isto não é json", _json_llm(diags)])
    rel = gerar_diagnostico(SAIDA_FASE2, _settings(), client=client)
    assert rel["llm_disponivel"] is True
    assert client.messages.chamadas == 2


def test_fallback_sem_api_key_nao_chama_rede():
    rel = gerar_diagnostico(SAIDA_FASE2, _settings(anthropic_api_key=None), client=None)
    assert rel["llm_disponivel"] is False
    assert "ANTHROPIC_API_KEY" in rel["aviso"]
    # Mesmo degradado, os números determinísticos seguem no relatório.
    assert rel["total_anomalias"] == 2
    assert rel["campanhas"][0]["anomalias"][0]["evidencia"]["variacao_pct"] == -95.9


def test_fallback_em_erro_de_api():
    client = FakeClient(anthropic.AnthropicError("API fora do ar"))
    rel = gerar_diagnostico(SAIDA_FASE2, _settings(), client=client)
    assert rel["llm_disponivel"] is False
    assert "degradado" in rel["aviso"].lower()
    assert client.messages.chamadas == 2  # tentou e desistiu


def test_auditoria_ignora_identificadores_mas_pega_numero_real(caplog):
    from src.llm import _auditar_numeros

    ident = {"CAMP-003", "Captacao", "Leads", "B2B"}
    # Só identificadores (B2B/CAMP-003 têm dígitos legítimos) -> NÃO avisa.
    with caplog.at_level("WARNING"):
        _auditar_numeros("Campanha B2B (CAMP-003) em colapso.", [], [], ident)
    assert "incluiu número" not in caplog.text
    # Número de verdade no texto -> AVISA.
    caplog.clear()
    with caplog.at_level("WARNING"):
        _auditar_numeros("O ROAS caiu 95% no período.", [], [], ident)
    assert "incluiu número" in caplog.text


def test_sem_anomalias_nao_precisa_de_llm():
    vazio = {"janela_dias": 7, "total_campanhas": 6, "total_anomalias": 0,
             "anomalias_por_severidade": {"crítica": 0, "alta": 0, "média": 0}, "anomalias": []}
    # Sem cliente e sem chave: ainda assim não degrada, porque não há o que diagnosticar.
    rel = gerar_diagnostico(vazio, _settings(anthropic_api_key=None), client=None)
    assert rel["llm_disponivel"] is True
    assert rel["campanhas"] == []
