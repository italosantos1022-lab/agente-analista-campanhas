"""Configuração tipada do projeto.

Toda a configuração vem do ambiente (`.env` na raiz ou variáveis de ambiente),
validada por pydantic-settings. Segredos NUNCA são hardcoded — ver `.env.example`.

As chaves de LLM e notificação são opcionais no carregamento para que as fases
iniciais (ingestão/análise) rodem sem `.env`. Cada fase que realmente precisa de
uma chave deve falhar rápido com mensagem clara no ponto de uso (ver fases 3 e 5).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Raiz do projeto = pasta que contém este `src/`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configuração central, carregada de `.env` + variáveis de ambiente."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Caminhos ---
    input_csv: Path = Field(
        default=PROJECT_ROOT / "data" / "dados-campanhas.csv",
        description="CSV de entrada com o relatório de Meta Ads.",
    )
    output_dir: Path = Field(
        default=PROJECT_ROOT / "output",
        description="Pasta onde os relatórios JSON/HTML são gravados.",
    )

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- LLM (Fase 3) ---
    anthropic_api_key: str | None = None
    llm_model_diagnostico: str = "claude-sonnet-4-6"
    llm_model_referencia: str = "claude-haiku-4-5-20251001"
    llm_temperature: float = 0.2
    llm_max_retries: int = 3

    # --- Notificação (Fase 5) ---
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    email_enabled: bool = False
    resend_api_key: str | None = None
    email_from: str | None = None
    email_to: str | None = None
    # Fallback SMTP documentado (usado se Resend não estiver configurado).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Retorna a configuração (singleton em cache para o processo)."""
    return Settings()
