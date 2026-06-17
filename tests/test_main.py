"""Testes da orquestração (Fase 6).

Herméticos: `_env_file=None` (não lê o .env real -> sem chave LLM -> degradado;
sem canais -> não envia) e `output_dir=tmp_path` (não sobrescreve os artefatos
reais em output/). Sem rede.
"""

from __future__ import annotations

import json

import src.main as m
from src.config import Settings
from src.main import executar_pipeline, main


def _cfg(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, output_dir=str(tmp_path), **kw)


def test_pipeline_completo_gera_todos_os_artefatos(tmp_path):
    resumo = executar_pipeline(_cfg(tmp_path), no_notify=True)
    assert resumo["total_anomalias"] == 10
    assert resumo["notificacao"] == "pulada"
    for nome in ("anomalias.json", "diagnostico.json", "relatorio.json", "relatorio.html"):
        assert (tmp_path / nome).exists(), f"faltou gerar {nome}"


def test_llm_degradado_nao_aborta_e_gera_relatorio(tmp_path):
    # Sem ANTHROPIC_API_KEY -> LLM degradado, mas o pipeline conclui com sucesso.
    resumo = executar_pipeline(_cfg(tmp_path), no_notify=True)
    assert resumo["llm_disponivel"] is False
    rel = json.loads((tmp_path / "relatorio.json").read_text(encoding="utf-8"))
    assert rel["llm_disponivel"] is False
    assert rel["aviso_llm"]                       # aviso claro de degradação
    assert rel["totais"]["anomalias"] == 10        # parte determinística intacta


def test_main_exit_0_em_sucesso(tmp_path):
    assert main(["--no-notify"], settings=_cfg(tmp_path)) == 0


def test_main_exit_0_com_llm_degradado(tmp_path):
    # LLM degradado é SUCESSO (exit 0), não falha.
    assert main(["--dry-run"], settings=_cfg(tmp_path)) == 0


def test_main_exit_nao_zero_em_csv_invalido(tmp_path):
    code = main(["--csv", str(tmp_path / "nao_existe.csv"), "--no-notify"],
                settings=_cfg(tmp_path))
    assert code == 1


def test_dry_run_gera_relatorio_mas_nao_envia(tmp_path):
    resumo = executar_pipeline(_cfg(tmp_path), dry_run=True)
    assert resumo["notificacao"] == "dry-run"
    assert resumo["notificacao_falhou_tudo"] is False
    assert (tmp_path / "relatorio.html").exists()  # relatório é gerado mesmo assim


def test_main_exit_1_em_excecao_inesperada(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(m, "executar_pipeline", _boom)
    assert main(["--no-notify"], settings=_cfg(tmp_path)) == 1


def test_main_exit_1_quando_toda_notificacao_falha(tmp_path, monkeypatch):
    # Relatório gerado, mas todos os canais falharam -> falha (exit 1).
    monkeypatch.setattr(
        m, "executar_pipeline",
        lambda *a, **k: {
            "linhas_validas": 42, "total_anomalias": 10, "llm_disponivel": True,
            "notificacao": {"telegram": "falha: x", "email": "falha: y"},
            "notificacao_falhou_tudo": True,
        },
    )
    assert main([], settings=_cfg(tmp_path)) == 1


def test_main_exit_0_quando_notificacao_parcial(tmp_path, monkeypatch):
    # 1 canal ok, 1 falhou -> isolamento funcionou -> sucesso (exit 0).
    monkeypatch.setattr(
        m, "executar_pipeline",
        lambda *a, **k: {
            "linhas_validas": 42, "total_anomalias": 10, "llm_disponivel": True,
            "notificacao": {"telegram": "ok", "email": "falha: y"},
            "notificacao_falhou_tudo": False,
        },
    )
    assert main([], settings=_cfg(tmp_path)) == 0
