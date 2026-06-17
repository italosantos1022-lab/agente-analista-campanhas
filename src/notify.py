"""Notificação (Fase 5). Interface Notifier + Telegram + Email + dispatcher.

Desenho (CLAUDE.md):
  - `Notifier`: interface; cada canal sabe se formatar e enviar.
  - `TelegramNotifier`: mensagem ENXUTA para celular (httpx -> Bot API).
  - `EmailNotifier`: corpo é o relatorio.html COMPLETO (Resend SDK, com SMTP de
    fallback documentado).
  - `Dispatcher`: lê do `.env` quais canais estão ligados e envia para todos.
    Cada canal é ISOLADO: se um falhar, o outro ainda vai e a falha fica logada.

Execução isolada (preview sem enviar):
    uv run python -m src.notify --dry-run
"""

from __future__ import annotations

import html as _html
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

import httpx

from src.config import Settings, get_settings
from src.logging_config import setup_logging
from src.report import METRICA_LABEL, _data_br

log = logging.getLogger("notify")


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
class Notifier(ABC):
    """Canal de notificação. `enviar` levanta exceção em caso de falha; o
    isolamento entre canais é responsabilidade do `Dispatcher`."""

    nome: str = "notifier"

    @abstractmethod
    def enviar(self, relatorio: dict, html: str) -> None:
        """Envia a notificação. Recebe o relatório (dict) e o HTML completo."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Telegram — mensagem enxuta para celular
# --------------------------------------------------------------------------- #
def _esc(texto) -> str:
    """Escapa texto para o parse_mode HTML do Telegram (mantém nossas tags)."""
    return _html.escape(str(texto), quote=False)


def montar_mensagem_telegram(relatorio: dict) -> str:
    """Resumo executivo + campanhas com anomalia CRÍTICA (campanha, o que
    aconteceu, severidade). Curto e formatado para celular."""
    periodo = relatorio.get("periodo") or {}
    linhas = ["📊 <b>Relatório de Campanhas</b>"]
    if periodo.get("inicio"):
        linhas.append(f"<i>{_data_br(periodo['inicio'])} a {_data_br(periodo['fim'])}</i>")
    linhas.append("")
    linhas.append(_esc(relatorio.get("resumo_executivo", "")))

    criticas = [
        (c, [a for a in c.get("anomalias", []) if a["severidade"] == "crítica"])
        for c in relatorio.get("campanhas", [])
    ]
    criticas = [(c, anoms) for c, anoms in criticas if anoms]

    if criticas:
        linhas.append("")
        linhas.append("🔴 <b>Anomalias críticas</b>")
        for c, anoms in criticas:
            linhas.append(f"<b>{_esc(c['campanha_id'])} — {_esc(c['campanha_nome'])}</b>")
            for a in anoms:
                rotulo = METRICA_LABEL.get(a["metrica"], a["metrica"])
                linhas.append(f"• {_esc(rotulo)}: {_esc(a['descricao'])} [{_esc(a['severidade'])}]")
    return "\n".join(linhas)


class TelegramNotifier(Notifier):
    nome = "telegram"

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None):
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._client = client  # injetável nos testes

    def enviar(self, relatorio: dict, html: str) -> None:
        if not self._token or not self._chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes")

        texto = montar_mensagem_telegram(relatorio)
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        client = self._client or httpx.Client(timeout=15)
        try:
            resposta = client.post(url, json=payload)
            resposta.raise_for_status()
        finally:
            if self._client is None:
                client.close()


# --------------------------------------------------------------------------- #
# Email — corpo é o relatorio.html completo
# --------------------------------------------------------------------------- #
def _montar_assunto(relatorio: dict) -> str:
    totais = relatorio.get("totais", {})
    por_sev = totais.get("por_severidade", {})
    return (
        f"[Campanhas] {totais.get('anomalias', 0)} anomalia(s), "
        f"{por_sev.get('crítica', 0)} crítica(s)"
    )


def _enviar_resend(settings: Settings, assunto: str, html: str) -> None:
    import resend  # import tardio: só é necessário se usar Resend

    resend.api_key = settings.resend_api_key
    resend.Emails.send(
        {
            "from": settings.email_from,
            "to": [e.strip() for e in settings.email_to.split(",")],
            "subject": assunto,
            "html": html,
        }
    )


def _enviar_smtp(settings: Settings, assunto: str, html: str) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = settings.email_from
    msg["To"] = settings.email_to
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as servidor:
        servidor.starttls()
        if settings.smtp_user:
            servidor.login(settings.smtp_user, settings.smtp_password or "")
        servidor.sendmail(
            settings.email_from,
            [e.strip() for e in settings.email_to.split(",")],
            msg.as_string(),
        )


class EmailNotifier(Notifier):
    nome = "email"

    def __init__(self, settings: Settings, sender: Optional[Callable[[str, str], None]] = None):
        self._s = settings
        self._sender = sender  # injetável nos testes: (assunto, html) -> None

    def enviar(self, relatorio: dict, html: str) -> None:
        if not self._s.email_from or not self._s.email_to:
            raise ValueError("EMAIL_FROM/EMAIL_TO ausentes")

        assunto = _montar_assunto(relatorio)
        if self._sender is not None:
            self._sender(assunto, html)
        elif self._s.resend_api_key:
            _enviar_resend(self._s, assunto, html)
        elif self._s.smtp_host:
            _enviar_smtp(self._s, assunto, html)  # fallback documentado
        else:
            raise ValueError(
                "Email ligado mas sem transporte: defina RESEND_API_KEY ou SMTP_HOST"
            )


# --------------------------------------------------------------------------- #
# Dispatcher — lê do .env quais canais estão ligados; cada canal isolado
# --------------------------------------------------------------------------- #
class Dispatcher:
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    @classmethod
    def from_settings(cls, settings: Settings) -> "Dispatcher":
        notifiers: list[Notifier] = []
        if settings.telegram_enabled:
            notifiers.append(TelegramNotifier(settings))
        if settings.email_enabled:
            notifiers.append(EmailNotifier(settings))
        return cls(notifiers)

    def enviar(self, relatorio: dict, html: str) -> dict[str, str]:
        """Envia por todos os canais. Falha de um NÃO impede os outros."""
        if not self.notifiers:
            log.warning("Nenhum canal de notificação ligado (.env).")
            return {}

        resultados: dict[str, str] = {}
        for notifier in self.notifiers:
            try:
                notifier.enviar(relatorio, html)
                resultados[notifier.nome] = "ok"
                log.info("Notificação enviada com sucesso: %s", notifier.nome)
            except Exception as exc:  # noqa: BLE001 — isolar canal, não derrubar os demais
                resultados[notifier.nome] = f"falha: {type(exc).__name__}: {exc}"
                log.error("Falha no canal %s (os demais continuam): %s", notifier.nome, exc)
        return resultados


# --------------------------------------------------------------------------- #
# Demo isolada da fase
# --------------------------------------------------------------------------- #
def _demo() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Notificação (Fase 5).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Não envia; só mostra o preview do Telegram e os canais ligados.",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    rel_json = settings.output_dir / "relatorio.json"
    rel_html = settings.output_dir / "relatorio.html"
    if not rel_json.exists() or not rel_html.exists():
        raise SystemExit("Rode a Fase 4 primeiro: uv run python -m src.report")

    relatorio = json.loads(rel_json.read_text(encoding="utf-8"))
    html = rel_html.read_text(encoding="utf-8")

    print(f"Canais ligados — telegram: {settings.telegram_enabled} | email: {settings.email_enabled}")
    print("\n--- PREVIEW da mensagem do Telegram ---\n")
    print(montar_mensagem_telegram(relatorio))
    print(f"\n--- Email: assunto = {_montar_assunto(relatorio)} | corpo = relatorio.html ---")

    if args.dry_run:
        print("\n[dry-run] Nada foi enviado.")
        return

    resultados = Dispatcher.from_settings(settings).enviar(relatorio, html)
    print(f"\nResultado do envio: {resultados or 'nenhum canal ligado'}")


if __name__ == "__main__":
    _demo()
