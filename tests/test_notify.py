"""Testes da notificação (Fase 5).

Foco no requisito central: cada canal é ISOLADO — se um falhar, o outro ainda
sai e a falha fica registrada. Telegram e Email são exercitados com fakes (sem
rede).
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.notify import (
    Dispatcher,
    EmailNotifier,
    Notifier,
    TelegramNotifier,
    montar_mensagem_telegram,
)

def _cfg(**kw) -> Settings:
    """Settings hermético: ignora o .env local para os testes não dependerem
    das credenciais reais da máquina."""
    return Settings(_env_file=None, **kw)


REL = {
    "periodo": {"inicio": "2026-06-01", "fim": "2026-06-07"},
    "resumo_executivo": "Quatro campanhas com anomalias críticas.",
    "totais": {"anomalias": 3, "por_severidade": {"crítica": 2, "alta": 1, "média": 0}},
    "campanhas": [
        {
            "campanha_id": "CAMP-002", "campanha_nome": "Remarketing Checkout",
            "objetivo": "Vendas", "status": "anomalias",
            "anomalias": [
                {"metrica": "roas", "severidade": "crítica",
                 "descricao": "ROAS caiu 95,9% no período (18,12 -> 0,74)."},
                {"metrica": "frequencia", "severidade": "alta",
                 "descricao": "Frequência atingiu 4,8."},
            ],
        },
        {
            "campanha_id": "CAMP-001", "campanha_nome": "Lancamento", "objetivo": "Vendas",
            "status": "estavel", "anomalias": [],
        },
    ],
}
HTML = "<html><body><h1>Relatório</h1></body></html>"


# --- Mensagem do Telegram -------------------------------------------------- #
def test_mensagem_telegram_tem_resumo_e_so_criticas():
    msg = montar_mensagem_telegram(REL)
    assert "Quatro campanhas" in msg          # resumo executivo
    assert "CAMP-002" in msg                   # campanha com crítica
    assert "ROAS" in msg and "crítica" in msg  # o que aconteceu + severidade
    assert "Frequência atingiu" not in msg     # anomalia 'alta' não entra
    assert "CAMP-001" not in msg               # campanha estável não entra
    assert "-&gt;" in msg                      # texto escapado p/ HTML do Telegram


# --- Telegram com client fake (sem rede) ----------------------------------- #
class _RespFake:
    def raise_for_status(self):
        return None


class _HttpxOk:
    def __init__(self):
        self.posts = []

    def post(self, url, json):
        self.posts.append((url, json))
        return _RespFake()


def test_telegram_posta_no_endpoint_certo():
    s = _cfg(telegram_bot_token="TOK", telegram_chat_id="CID")
    fake = _HttpxOk()
    TelegramNotifier(s, client=fake).enviar(REL, HTML)
    url, payload = fake.posts[0]
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert payload["chat_id"] == "CID"
    assert payload["parse_mode"] == "HTML"
    assert "CAMP-002" in payload["text"]


def test_telegram_sem_credencial_levanta():
    s = _cfg(telegram_bot_token=None, telegram_chat_id=None)
    with pytest.raises(ValueError):
        TelegramNotifier(s).enviar(REL, HTML)


# --- Email com sender fake ------------------------------------------------- #
def test_email_envia_html_completo():
    enviados = []
    s = _cfg(email_from="a@x.com", email_to="b@y.com")
    EmailNotifier(s, sender=lambda assunto, html: enviados.append((assunto, html))).enviar(REL, HTML)
    assunto, corpo = enviados[0]
    assert corpo == HTML                  # corpo é o relatorio.html completo
    assert "crítica" in assunto           # assunto resume a severidade


def test_email_sem_transporte_levanta():
    # Ligado, com from/to, mas sem Resend e sem SMTP -> erro claro.
    s = _cfg(email_from="a@x.com", email_to="b@y.com")
    with pytest.raises(ValueError):
        EmailNotifier(s).enviar(REL, HTML)


# --- Dispatcher: lê flags + ISOLAMENTO ------------------------------------- #
def test_dispatcher_le_flags_do_env():
    s = _cfg(telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="c",
                 email_enabled=False)
    disp = Dispatcher.from_settings(s)
    assert {n.nome for n in disp.notifiers} == {"telegram"}


def test_dispatcher_sem_canais_nao_quebra():
    s = _cfg(telegram_enabled=False, email_enabled=False)
    assert Dispatcher.from_settings(s).enviar(REL, HTML) == {}


class _NotifierFake(Notifier):
    def __init__(self, nome, falhar, registro):
        self.nome = nome
        self._falhar = falhar
        self._registro = registro

    def enviar(self, relatorio, html):
        if self._falhar:
            raise RuntimeError("canal fora do ar")
        self._registro.append(self.nome)


def test_canal_que_falha_nao_impede_o_outro():
    enviados = []
    ruim = _NotifierFake("ruim", True, enviados)
    bom = _NotifierFake("bom", False, enviados)
    # 'ruim' primeiro: mesmo falhando, 'bom' tem que sair.
    resultados = Dispatcher([ruim, bom]).enviar(REL, HTML)
    assert resultados["ruim"].startswith("falha")
    assert resultados["bom"] == "ok"
    assert enviados == ["bom"]


def test_isolamento_telegram_falha_email_sai():
    # Caso realista: Telegram cai na rede, Email (sender fake) ainda envia.
    class _HttpxBoom:
        def post(self, url, json):
            raise RuntimeError("rede caiu")

    s = _cfg(telegram_bot_token="T", telegram_chat_id="C",
                 email_from="a@x.com", email_to="b@y.com")
    enviados = []
    tg = TelegramNotifier(s, client=_HttpxBoom())
    em = EmailNotifier(s, sender=lambda assunto, html: enviados.append(html))
    resultados = Dispatcher([tg, em]).enviar(REL, HTML)
    assert resultados["telegram"].startswith("falha")
    assert resultados["email"] == "ok"
    assert enviados == [HTML]
