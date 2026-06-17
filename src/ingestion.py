"""Ingestão e validação do CSV de campanhas (Fase 1).

Responsabilidade: transformar o CSV cru (com armadilhas) numa lista de
`CampanhaRow` validadas, logando CADA limpeza feita e separando vazio esperado
de dado faltando. CSV malformado falha com `IngestionError` (mensagem clara),
nunca com stack trace cru.

Armadilhas tratadas (ver CLAUDE.md):
  1. `gasto` em formato BR "1.272,60"  -> normaliza para float.
  2. `cliques_link` negativo (-1005)    -> corrige sinal (abs) + flag.
  3. Linha exatamente duplicada         -> deduplica.
  4. Conversão faltando c/ checkout      -> marca incompleto, não quebra.
  5. `leads = 0` por vários dias         -> mantém 0 (proteção div/zero a jusante).
  6. Vazios esperados por objetivo       -> não confunde com dado faltando.

Execução isolada (demo da fase):
    uv run python -m src.ingestion
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from src.config import get_settings
from src.logging_config import setup_logging
from src.models import CampanhaRow

log = logging.getLogger("ingestion")

# Esquema esperado do relatório de Meta Ads. Todas obrigatórias na estrutura
# (algumas células podem estar vazias; a coluna em si precisa existir).
COLUNAS_ESPERADAS: tuple[str, ...] = (
    "data",
    "campanha_id",
    "campanha_nome",
    "objetivo",
    "plataforma",
    "alcance",
    "impressoes",
    "frequencia",
    "gasto",
    "cpm",
    "cliques_todos",
    "ctr_todos",
    "cliques_link",
    "ctr_link",
    "cpc_link",
    "visualizacoes_pagina_destino",
    "video_3s",
    "thruplays",
    "adicoes_carrinho",
    "checkouts_iniciados",
    "compras",
    "custo_por_compra",
    "valor_compras",
    "roas",
    "leads",
    "custo_por_lead",
    "conversas_iniciadas",
    "custo_por_conversa",
)

# Tipagem por coluna para conversão controlada (string -> python).
COLUNAS_INT: frozenset[str] = frozenset(
    {
        "alcance",
        "impressoes",
        "cliques_todos",
        "cliques_link",
        "visualizacoes_pagina_destino",
        "video_3s",
        "thruplays",
        "adicoes_carrinho",
        "checkouts_iniciados",
        "compras",
        "leads",
        "conversas_iniciadas",
    }
)
COLUNAS_FLOAT: frozenset[str] = frozenset(
    {
        "frequencia",
        "gasto",
        "cpm",
        "ctr_todos",
        "ctr_link",
        "cpc_link",
        "custo_por_compra",
        "valor_compras",
        "roas",
        "custo_por_lead",
        "custo_por_conversa",
    }
)
# Colunas de texto / repassadas direto ao pydantic (data e objetivo viram
# date/enum dentro do modelo).
COLUNAS_TEXTO: frozenset[str] = frozenset(
    {"data", "campanha_id", "campanha_nome", "objetivo", "plataforma"}
)


class IngestionError(Exception):
    """Erro de ingestão com mensagem clara (CSV malformado, etc.)."""


@dataclass
class IngestionResult:
    """Resultado da ingestão: linhas válidas, inválidas e um resumo."""

    linhas: list[CampanhaRow]
    invalidas: list[dict]
    resumo: dict = field(default_factory=dict)

    def to_dataframe(self) -> pd.DataFrame:
        """DataFrame das linhas válidas (útil para a Fase 2).

        `mode="json"` serializa enum -> valor ("Vendas") e date -> ISO string,
        deixando o DataFrame legível e pronto para persistência.
        """
        return pd.DataFrame([r.model_dump(mode="json") for r in self.linhas])


# --------------------------------------------------------------------------- #
# Leitura crua + validação estrutural
# --------------------------------------------------------------------------- #
def _ler_csv(caminho: Path) -> pd.DataFrame:
    """Lê o CSV como texto puro, falhando claro se estiver malformado."""
    if not caminho.exists():
        raise IngestionError(f"Arquivo não encontrado: {caminho}")
    if caminho.stat().st_size == 0:
        raise IngestionError(f"CSV vazio (0 bytes): {caminho}")

    try:
        # dtype=str + keep_default_na=False: nós controlamos a conversão e o
        # que é "vazio" (string ""), em vez de deixar o pandas inferir NaN.
        df = pd.read_csv(
            caminho,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except pd.errors.EmptyDataError as exc:
        raise IngestionError("CSV sem cabeçalho/conteúdo legível.") from exc
    except UnicodeDecodeError as exc:
        raise IngestionError(
            "Encoding inválido: o arquivo não está em UTF-8."
        ) from exc
    except pd.errors.ParserError as exc:
        raise IngestionError(f"CSV malformado (erro de parsing): {exc}") from exc

    df.columns = [c.strip() for c in df.columns]

    if df.empty:
        raise IngestionError("CSV tem cabeçalho mas nenhuma linha de dados.")

    faltando = [c for c in COLUNAS_ESPERADAS if c not in df.columns]
    if faltando:
        raise IngestionError(
            "Colunas obrigatórias ausentes: " + ", ".join(faltando)
        )

    extras = [c for c in df.columns if c not in COLUNAS_ESPERADAS]
    if extras:
        log.warning("Colunas extras ignoradas: %s", ", ".join(extras))

    log.info("CSV lido: %d linhas brutas, %d colunas.", len(df), len(df.columns))
    return df


# --------------------------------------------------------------------------- #
# Limpezas (cada uma loga o que fez)
# --------------------------------------------------------------------------- #
def _normaliza_gasto_br(valor: str) -> str:
    """Converte "1.272,60" -> "1272.60". Sem vírgula, devolve inalterado."""
    return valor.replace(".", "").replace(",", ".")


def _deduplicar(df: pd.DataFrame, resumo: dict) -> pd.DataFrame:
    """Armadilha 3: remove linhas exatamente duplicadas."""
    dup_mask = df.duplicated(keep="first")
    n = int(dup_mask.sum())
    resumo["duplicatas_removidas"] = n
    if n:
        for _, linha in df[dup_mask].iterrows():
            log.info(
                "LIMPEZA duplicata removida | %s %s (linha idêntica)",
                linha["campanha_id"],
                linha["data"],
            )
        df = df[~dup_mask].reset_index(drop=True)
    return df


def _normalizar_gasto(df: pd.DataFrame, flags: dict[int, list[str]], resumo: dict) -> None:
    """Armadilha 1: gasto em formato BR -> ponto decimal."""
    br_mask = df["gasto"].str.contains(",", regex=False, na=False)
    resumo["gasto_formato_br_corrigido"] = int(br_mask.sum())
    for idx in df.index[br_mask]:
        original = df.at[idx, "gasto"]
        novo = _normaliza_gasto_br(original)
        df.at[idx, "gasto"] = novo
        flags[idx].append("gasto_normalizado_formato_br")
        log.info(
            "LIMPEZA gasto formato BR | %s %s | '%s' -> %s",
            df.at[idx, "campanha_id"],
            df.at[idx, "data"],
            original,
            novo,
        )


def _corrigir_cliques_negativos(
    df: pd.DataFrame, flags: dict[int, list[str]], resumo: dict
) -> None:
    """Armadilha 2: cliques_link negativo -> corrige sinal (abs) + flag."""
    n = 0
    for idx in df.index:
        bruto = df.at[idx, "cliques_link"].strip()
        if bruto.lstrip("-").isdigit() and int(bruto) < 0:
            corrigido = abs(int(bruto))
            df.at[idx, "cliques_link"] = str(corrigido)
            flags[idx].append("cliques_link_negativo_corrigido")
            n += 1
            log.warning(
                "LIMPEZA cliques_link negativo | %s %s | %s -> %d (sinal corrigido)",
                df.at[idx, "campanha_id"],
                df.at[idx, "data"],
                bruto,
                corrigido,
            )
    resumo["cliques_link_negativos_corrigidos"] = n


# --------------------------------------------------------------------------- #
# Conversão de tipos + validação por linha
# --------------------------------------------------------------------------- #
def _converter_celula(coluna: str, bruto: str) -> object:
    """Converte uma célula string para o tipo da coluna ('' -> None)."""
    valor = bruto.strip()
    if coluna in COLUNAS_TEXTO:
        return valor  # data/objetivo são parseados pelo pydantic
    if valor == "":
        return None
    if coluna in COLUNAS_INT:
        return int(valor)
    if coluna in COLUNAS_FLOAT:
        return float(valor)
    return valor


def _montar_registro(linha: pd.Series, flags: list[str]) -> dict:
    """Monta o dict tipado para o pydantic a partir de uma linha do df."""
    registro: dict[str, object] = {}
    for coluna in COLUNAS_ESPERADAS:
        registro[coluna] = _converter_celula(coluna, linha[coluna])
    registro["flags_limpeza"] = flags
    return registro


def _validar_linhas(
    df: pd.DataFrame, flags: dict[int, list[str]], resumo: dict
) -> tuple[list[CampanhaRow], list[dict]]:
    """Valida cada linha com pydantic; coleta válidas e inválidas."""
    validas: list[CampanhaRow] = []
    invalidas: list[dict] = []

    for idx, linha in df.iterrows():
        try:
            registro = _montar_registro(linha, flags.get(idx, []))
            row = CampanhaRow.model_validate(registro)
        except (ValidationError, ValueError) as exc:
            motivo = str(exc).splitlines()[0]
            invalidas.append(
                {
                    "linha_csv": int(idx) + 2,  # +2: cabeçalho + base 0
                    "campanha_id": linha.get("campanha_id", "?"),
                    "data": linha.get("data", "?"),
                    "erro": motivo,
                }
            )
            log.error(
                "INVÁLIDA linha %d | %s %s | %s",
                int(idx) + 2,
                linha.get("campanha_id", "?"),
                linha.get("data", "?"),
                motivo,
            )
            continue

        validas.append(row)
        if row.incompleto:
            log.warning(
                "INCOMPLETA %s %s (%s) | faltando: %s",
                row.campanha_id,
                row.data,
                row.objetivo.value,
                ", ".join(row.campos_faltando),
            )

    resumo["linhas_validas"] = len(validas)
    resumo["linhas_invalidas"] = len(invalidas)
    resumo["linhas_incompletas"] = sum(1 for r in validas if r.incompleto)
    return validas, invalidas


# --------------------------------------------------------------------------- #
# Orquestração da ingestão
# --------------------------------------------------------------------------- #
def ingerir(caminho: Path) -> IngestionResult:
    """Pipeline completo: lê, limpa, valida e devolve o resultado."""
    log.info("Iniciando ingestão: %s", caminho)
    resumo: dict = {}

    df = _ler_csv(caminho)
    resumo["linhas_brutas"] = len(df)

    df = _deduplicar(df, resumo)
    flags: dict[int, list[str]] = {idx: [] for idx in df.index}

    _normalizar_gasto(df, flags, resumo)
    _corrigir_cliques_negativos(df, flags, resumo)

    validas, invalidas = _validar_linhas(df, flags, resumo)

    log.info(
        "Ingestão concluída | válidas=%d incompletas=%d inválidas=%d "
        "| dedup=%d gasto_br=%d cliques_neg=%d",
        resumo["linhas_validas"],
        resumo["linhas_incompletas"],
        resumo["linhas_invalidas"],
        resumo["duplicatas_removidas"],
        resumo["gasto_formato_br_corrigido"],
        resumo["cliques_link_negativos_corrigidos"],
    )
    return IngestionResult(linhas=validas, invalidas=invalidas, resumo=resumo)


# --------------------------------------------------------------------------- #
# Demo isolada da fase
# --------------------------------------------------------------------------- #
def _demo() -> None:
    parser = argparse.ArgumentParser(
        description="Ingestão e validação do CSV de campanhas (Fase 1)."
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
    caminho = args.csv or settings.input_csv

    try:
        resultado = ingerir(caminho)
    except IngestionError as exc:
        log.error("Falha na ingestão: %s", exc)
        raise SystemExit(1) from exc

    print("\n" + "=" * 78)
    print("RESUMO DA INGESTÃO")
    print("=" * 78)
    for chave, valor in resultado.resumo.items():
        print(f"  {chave:32} : {valor}")

    df = resultado.to_dataframe()

    print("\n" + "=" * 78)
    print("AMOSTRA — métricas de entrega (primeiras 5 linhas)")
    print("=" * 78)
    cols_entrega = [
        "data",
        "campanha_id",
        "objetivo",
        "gasto",
        "cliques_link",
        "ctr_link",
        "frequencia",
    ]
    print(df[cols_entrega].head().to_string(index=False))

    print("\n" + "=" * 78)
    print("DESTAQUE DAS ARMADILHAS TRATADAS")
    print("=" * 78)

    # 1 e 2: linhas que receberam flag de limpeza.
    com_flag = df[df["flags_limpeza"].map(bool)]
    print("\n[1+2] Linhas limpas (gasto BR / cliques negativo):")
    if not com_flag.empty:
        print(
            com_flag[
                ["data", "campanha_id", "gasto", "cliques_link", "flags_limpeza"]
            ].to_string(index=False)
        )

    # 4: linhas incompletas (dado faltando, não vazio esperado).
    incompletas = df[df["incompleto"]]
    print("\n[4] Linhas INCOMPLETAS (resultado faltando p/ o objetivo):")
    if not incompletas.empty:
        print(
            incompletas[
                ["data", "campanha_id", "objetivo", "campos_faltando"]
            ].to_string(index=False)
        )

    # 5: leads = 0 preservado (não vira NaN, custo_por_lead vazio é esperado).
    leads_zero = df[(df["objetivo"] == "Leads") & (df["leads"] == 0)]
    print("\n[5] leads = 0 preservado (custo_por_lead vazio = esperado):")
    if not leads_zero.empty:
        print(
            leads_zero[
                ["data", "campanha_id", "leads", "custo_por_lead", "campos_vazios_esperados"]
            ].to_string(index=False)
        )

    # 6: vazio esperado por objetivo (Reconhecimento não tem conversão).
    recon = df[df["objetivo"] == "Reconhecimento"].head(1)
    print("\n[6] Vazio ESPERADO por objetivo (ex.: Reconhecimento):")
    if not recon.empty:
        print(
            recon[
                ["data", "campanha_id", "objetivo", "campos_vazios_esperados"]
            ].to_string(index=False)
        )

    print()


if __name__ == "__main__":
    _demo()
