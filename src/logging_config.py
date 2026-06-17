"""Configuração de logging estruturado (stdlib).

Um único ponto para configurar o logging do projeto, para que cada etapa
(ingestão, análise, LLM, notificação) logue de forma consistente e auditável,
inclusive em execução não supervisionada.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """Configura o logger raiz para escrever em stdout com formato fixo.

    Idempotente: limpa handlers anteriores para não duplicar linhas se
    chamado mais de uma vez (ex.: em testes).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Atalho para `logging.getLogger`, mantendo o estilo do projeto."""
    return logging.getLogger(name)
