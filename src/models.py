"""Schemas pydantic do projeto.

- Fase 1: `CampanhaRow` (linha limpa e validada).
- Fase 2: `Anomalia` + enums `Severidade`/`TipoAnomalia` (saída da análise
  determinística). O schema do relatório final entra na Fase 4.

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


# =========================================================================== #
# Fase 2 — Anomalias (saída da análise determinística)
# =========================================================================== #
class Severidade(str, Enum):
    """Severidade atribuída por REGRA na camada determinística (não pelo LLM)."""

    CRITICA = "crítica"
    ALTA = "alta"
    MEDIA = "média"


class TipoAnomalia(str, Enum):
    """Tipos de anomalia detectáveis (a régua aplicada depende do objetivo)."""

    ROAS_EM_QUEDA = "roas_em_queda"
    CUSTO_POR_COMPRA_ALTO = "custo_por_compra_alto"
    CTR_EM_QUEDA = "ctr_em_queda"
    FREQUENCIA_ALTA = "frequencia_alta"  # fadiga de criativo
    CUSTO_POR_LEAD_ALTO = "custo_por_lead_alto"
    VOLUME_LEADS_COLAPSO = "volume_leads_colapso"
    CUSTO_POR_CONVERSA_ALTO = "custo_por_conversa_alto"
    VOLUME_CONVERSAS_COLAPSO = "volume_conversas_colapso"
    ALCANCE_COLAPSO = "alcance_colapso"
    CPM_ALTO = "cpm_alto"


class Anomalia(BaseModel):
    """Uma anomalia detectada, com os números que a comprovam.

    `descricao` é um texto FACTUAL gerado pelo código (o quê + números). O
    diagnóstico em linguagem natural é responsabilidade do LLM na Fase 3 — aqui
    nenhuma IA participa.
    """

    campanha_id: str
    campanha_nome: str
    objetivo: Objetivo
    metrica: str  # métrica-base (ex.: "roas", "custo_por_compra", "alcance")
    tipo: TipoAnomalia
    descricao: str
    severidade: Severidade
    # Números que comprovam (primeiro/último dia, variação %, baseline, etc.).
    evidencia: dict


# =========================================================================== #
# Fase 3 — Camada LLM (o LLM só escreve TEXTO; NUNCA números)
# =========================================================================== #
class AcaoRecomendada(str, Enum):
    """Conjunto FECHADO de ações que o LLM pode recomendar.

    Modelado como enum de propósito: qualquer ação fora desta lista faz a
    validação pydantic falhar — o LLM não consegue "inventar" uma ação nova.
    """

    PAUSAR_CRIATIVO = "pausar_criativo"
    RENOVAR_CRIATIVO = "renovar_criativo"
    EXPANDIR_PUBLICO = "expandir_publico"
    TROCAR_PUBLICO = "trocar_publico"
    REVISAR_OFERTA = "revisar_oferta"
    INVESTIGAR_FORMULARIO = "investigar_formulario"
    INVESTIGAR_INTEGRACAO = "investigar_integracao"
    REALOCAR_VERBA = "realocar_verba"


class DiagnosticoAnomalia(BaseModel):
    """Diagnóstico textual de UMA anomalia + ação recomendada (saída do LLM).

    `ref` é o índice da anomalia na entrada (correlação determinística). Não há
    campo numérico aqui: o LLM apenas escreve texto e escolhe uma ação.
    """

    model_config = ConfigDict(extra="ignore")

    ref: int
    campanha_id: str
    metrica: str
    diagnostico: str
    acao: AcaoRecomendada
    justificativa_acao: str


class NarrativaCampanha(BaseModel):
    """Narrativa que consolida as anomalias de UMA campanha (saída do LLM)."""

    model_config = ConfigDict(extra="ignore")

    campanha_id: str
    narrativa: str


class DiagnosticoLLM(BaseModel):
    """Resposta completa esperada do LLM, validada por pydantic."""

    model_config = ConfigDict(extra="ignore")

    resumo_executivo: str
    narrativas: list[NarrativaCampanha]
    diagnosticos: list[DiagnosticoAnomalia]

