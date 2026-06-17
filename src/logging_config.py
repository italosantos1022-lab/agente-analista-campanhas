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
    # Garante UTF-8 no console de forma portável. No Windows o console usa
    # cp1252 por padrão e quebra (UnicodeEncodeError) ao logar mensagens com
    # emoji — como o preview do Telegram em --dry-run. Reconfigurar o stream
    # para UTF-8 resolve sem mexer no conteúdo da mensagem. A guarda existe
    # porque nem todo stream expõe reconfigure (ex.: o pytest substitui
    # stdout/stderr por objetos que não têm o método).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    root.addHandler(handler)

    # Silencia libs HTTP: além de ruidosas, o httpx loga a URL completa — que no
    # caso da Bot API do Telegram CONTÉM o token. Mantê-las em WARNING evita
    # vazar segredo no log.
    for ruidoso in ("httpx", "httpcore", "urllib3", "requests"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Atalho para `logging.getLogger`, mantendo o estilo do projeto."""
    return logging.getLogger(name)
