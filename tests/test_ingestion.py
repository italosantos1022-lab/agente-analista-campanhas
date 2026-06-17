"""Testes da ingestão (Fase 1): robustez contra CSV malformado e as 6 armadilhas.

Roda com: uv run pytest
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ingestion import IngestionError, ingerir

HEADER = (
    "data,campanha_id,campanha_nome,objetivo,plataforma,alcance,impressoes,"
    "frequencia,gasto,cpm,cliques_todos,ctr_todos,cliques_link,ctr_link,"
    "cpc_link,visualizacoes_pagina_destino,video_3s,thruplays,adicoes_carrinho,"
    "checkouts_iniciados,compras,custo_por_compra,valor_compras,roas,leads,"
    "custo_por_lead,conversas_iniciadas,custo_por_conversa"
)


def _linha_vendas(data="2026-06-01", camp="CAMP-001", gasto="850.40", cliques="995",
                  compras="42", cpc="20.25", valor="12990.10", roas="15.28"):
    return (
        f"{data},{camp},Curso,Vendas,Meta Ads,23795,45210,1.90,{gasto},18.81,1669,"
        f"3.69,{cliques},2.20,0.85,779,15055,4551,134,73,{compras},{cpc},{valor},"
        f"{roas},,,,"
    )


def _escrever(tmp_path: Path, conteudo: str, *, nome="t.csv", binario=False) -> Path:
    p = tmp_path / nome
    if binario:
        p.write_bytes(conteudo)
    else:
        p.write_text(conteudo, encoding="utf-8")
    return p


# --- CSV malformado: erro claro, não stack trace cru ----------------------- #
def test_arquivo_inexistente(tmp_path):
    with pytest.raises(IngestionError, match="não encontrado"):
        ingerir(tmp_path / "nao_existe.csv")


def test_arquivo_vazio(tmp_path):
    with pytest.raises(IngestionError, match="vazio"):
        ingerir(_escrever(tmp_path, ""))


def test_so_cabecalho(tmp_path):
    with pytest.raises(IngestionError, match="nenhuma linha"):
        ingerir(_escrever(tmp_path, HEADER + "\n"))


def test_coluna_obrigatoria_ausente(tmp_path):
    with pytest.raises(IngestionError, match="Colunas obrigatórias ausentes"):
        ingerir(_escrever(tmp_path, "data,campanha_id\n2026-06-01,CAMP-001\n"))


def test_encoding_invalido(tmp_path):
    conteudo = HEADER.encode("utf-8") + b"\n" + b"2026-06-01,\xff\xfe lixo\n"
    with pytest.raises(IngestionError, match="Encoding|UTF-8"):
        ingerir(_escrever(tmp_path, conteudo, binario=True))


# --- Armadilhas ------------------------------------------------------------ #
def test_gasto_formato_br(tmp_path):
    csv = HEADER + "\n" + _linha_vendas(gasto='"1.272,60"')
    res = ingerir(_escrever(tmp_path, csv))
    assert res.linhas[0].gasto == pytest.approx(1272.60)
    assert "gasto_normalizado_formato_br" in res.linhas[0].flags_limpeza


def test_cliques_link_negativo_corrigido(tmp_path):
    csv = HEADER + "\n" + _linha_vendas(cliques="-1005")
    res = ingerir(_escrever(tmp_path, csv))
    assert res.linhas[0].cliques_link == 1005
    assert "cliques_link_negativo_corrigido" in res.linhas[0].flags_limpeza


def test_duplicata_removida(tmp_path):
    linha = _linha_vendas()
    csv = HEADER + "\n" + linha + "\n" + linha + "\n"
    res = ingerir(_escrever(tmp_path, csv))
    assert res.resumo["duplicatas_removidas"] == 1
    assert len(res.linhas) == 1


def test_conversao_faltando_marca_incompleto(tmp_path):
    # Vendas com checkout mas sem compras/valor/roas -> incompleto.
    csv = HEADER + "\n" + _linha_vendas(compras="", cpc="", valor="", roas="")
    res = ingerir(_escrever(tmp_path, csv))
    linha = res.linhas[0]
    assert linha.incompleto is True
    assert "compras" in linha.campos_faltando


def test_leads_zero_nao_e_faltando(tmp_path):
    # Leads = 0: valor real (não faltando); custo_por_lead vazio = esperado.
    linha = (
        "2026-06-04,CAMP-003,B2B,Leads,Meta Ads,3538,9200,2.60,435.00,47.28,286,"
        "3.11,166,1.80,2.62,134,3313,987,,,,,,,0,,,"
    )
    res = ingerir(_escrever(tmp_path, HEADER + "\n" + linha + "\n"))
    row = res.linhas[0]
    assert row.leads == 0
    assert row.incompleto is False
    assert "custo_por_lead" in row.campos_vazios_esperados


def test_vazio_esperado_por_objetivo(tmp_path):
    # Reconhecimento não tem conversão: tudo vazio é esperado, nada faltando.
    linha = (
        "2026-06-01,CAMP-005,Video,Reconhecimento,Meta Ads,54444,98000,1.80,540.00,"
        "5.51,1712,1.75,1078,1.10,0.50,851,34469,11111,,,,,,,,,,"
    )
    res = ingerir(_escrever(tmp_path, HEADER + "\n" + linha + "\n"))
    row = res.linhas[0]
    assert row.incompleto is False
    assert row.campos_faltando == []
    assert "compras" in row.campos_vazios_esperados
