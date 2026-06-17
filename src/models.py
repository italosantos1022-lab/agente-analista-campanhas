"""Schemas pydantic do projeto.

Por enquanto (Fase 1) só a linha de campanha. Os schemas de anomalia e de
relatório entram nas fases 2–4.

Conceito central da Fase 1: distinguir **vazio esperado** de **dado faltando**.
Cada objetivo de campanha preenche um subconjunto diferente das colunas de
resultado; as demais ficam vazias *por design* e NÃO podem virar alerta falso.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Objetivo(str, Enum):
    """Objetivos de campanha suportados (régua de métricas é por objetivo)."""

    VENDAS = "Vendas"
    LEADS = "Leads"
    MENSAGENS = "Mensagens"
    RECONHECIMENTO = "Reconhecimento"


# Colunas de RESULTADO que dependem do objetivo — podem estar vazias por design.
COLUNAS_RESULTADO: frozenset[str] = frozenset(
    {
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
    }
)

# Para cada objetivo, quais métricas de resultado SÃO esperadas (relevantes).
# O que não está aqui, para aquele objetivo, é vazio esperado.
METRICAS_RELEVANTES: dict[Objetivo, frozenset[str]] = {
    Objetivo.VENDAS: frozenset(
        {
            "adicoes_carrinho",
            "checkouts_iniciados",
            "compras",
            "custo_por_compra",
            "valor_compras",
            "roas",
        }
    ),
    Objetivo.LEADS: frozenset({"leads", "custo_por_lead"}),
    Objetivo.MENSAGENS: frozenset({"conversas_iniciadas", "custo_por_conversa"}),
    Objetivo.RECONHECIMENTO: frozenset(),  # só métricas de entrega comuns
}

# Métricas derivadas (custo/ratio) e a contagem-base correspondente.
# Se a base é exatamente 0, o derivado vazio é ESPERADO (proteção contra
# divisão por zero), não dado faltando. Se a base está faltando (None), aí sim
# o bloco inteiro é considerado incompleto.
DERIVADAS_BASE: dict[str, str] = {
    "custo_por_compra": "compras",
    "valor_compras": "compras",
    "roas": "compras",
    "custo_por_lead": "leads",
    "custo_por_conversa": "conversas_iniciadas",
}

NonNegInt = Annotated[int, Field(ge=0)]
NonNegFloat = Annotated[float, Field(ge=0)]


class CampanhaRow(BaseModel):
    """Uma linha (campanha × dia) já limpa e validada.

    Campos de entrega são sempre obrigatórios. Campos de resultado são opcionais
    e sua ausência é classificada em `campos_faltando` (problema) vs
    `campos_vazios_esperados` (normal para o objetivo).
    """

    model_config = ConfigDict(extra="forbid")

    # --- Identificação (sempre presente) ---
    data: date
    campanha_id: str
    campanha_nome: str
    objetivo: Objetivo
    plataforma: str

    # --- Métricas de entrega (sempre presentes) ---
    alcance: NonNegInt
    impressoes: NonNegInt
    frequencia: NonNegFloat
    gasto: NonNegFloat
    cpm: NonNegFloat
    cliques_todos: NonNegInt
    ctr_todos: NonNegFloat
    cliques_link: NonNegInt
    ctr_link: NonNegFloat
    cpc_link: NonNegFloat
    visualizacoes_pagina_destino: NonNegInt
    video_3s: NonNegInt
    thruplays: NonNegInt

    # --- Métricas de resultado (dependem do objetivo) ---
    adicoes_carrinho: Optional[NonNegInt] = None
    checkouts_iniciados: Optional[NonNegInt] = None
    compras: Optional[NonNegInt] = None
    custo_por_compra: Optional[NonNegFloat] = None
    valor_compras: Optional[NonNegFloat] = None
    roas: Optional[NonNegFloat] = None
    leads: Optional[NonNegInt] = None
    custo_por_lead: Optional[NonNegFloat] = None
    conversas_iniciadas: Optional[NonNegInt] = None
    custo_por_conversa: Optional[NonNegFloat] = None

    # --- Metadados de qualidade (injetados/computados na ingestão) ---
    flags_limpeza: list[str] = Field(default_factory=list)
    incompleto: bool = False
    campos_faltando: list[str] = Field(default_factory=list)
    campos_vazios_esperados: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _classificar_vazios(self) -> "CampanhaRow":
        """Separa vazio esperado de dado faltando, por objetivo."""
        relevantes = METRICAS_RELEVANTES[self.objetivo]
        faltando: list[str] = []
        esperados: list[str] = []

        for col in COLUNAS_RESULTADO:
            valor = getattr(self, col)
            if col in relevantes:
                if valor is None:
                    base_col = DERIVADAS_BASE.get(col)
                    if base_col is not None and getattr(self, base_col) == 0:
                        # Derivado vazio porque a base é 0: esperado (div/zero).
                        esperados.append(col)
                    else:
                        faltando.append(col)
            else:
                # Coluna não-relevante para este objetivo: vazia é o esperado.
                if valor is None:
                    esperados.append(col)

        self.campos_faltando = sorted(faltando)
        self.campos_vazios_esperados = sorted(esperados)
        self.incompleto = bool(faltando)
        return self
