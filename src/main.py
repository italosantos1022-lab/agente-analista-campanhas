"""Orquestração ponta a ponta (Fase 6). Ponto de entrada único.

Encadeia, em sequência: ingestão -> análise determinística -> camada LLM ->
relatório -> notificação. Um comando roda do CSV à notificação:

    uv run python -m src.main
    uv run python -m src.main --csv outro.csv
    uv run python -m src.main --dry-run     # gera tudo, NÃO envia (loga o que sairia)
    uv run python -m src.main --no-notify   # pula a notificação

Contratos importantes (para cron / GitHub Actions):
  - Exit 0 em sucesso; não-zero em falha. Qualquer falha não tratada vira uma
    mensagem de erro CLARA (sem stack trace cru) + exit 1.
  - Se o LLM falhar, o pipeline NÃO aborta: gera o relatório DEGRADADO (só a
    parte determinística) e notifica mesmo assim. Isso é SUCESSO.
  - Falha total de notificação (todos os canais ligados falharam) conta como
    falha (exit 1), pois "do CSV à notificação" inclui a entrega. Falha parcial
    (≥1 canal ok) é sucesso — é o isolamento por canal funcionando.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

from src.analysis import analisar, construir_saida
from src.config import Settings, get_settings
from src.ingestion import IngestionError, ingerir
from src.llm import gerar_diagnostico
from src.logging_config import setup_logging
from src.notify import (
    Dispatcher,
    _montar_assunto,
    montar_mensagem_telegram,
)
from src.report import construir_relatorio, renderizar_html

log = logging.getLogger("main")


# --------------------------------------------------------------------------- #
# Etapa de notificação (isolada para clareza)
# --------------------------------------------------------------------------- #
def _etapa_notificacao(
    settings: Settings, relatorio: dict, html: str, dry_run: bool, no_notify: bool
) -> tuple[object, bool]:
    """Executa (ou simula) a notificação. Retorna (detalhe, falhou_tudo)."""
    if no_notify:
        log.info("FASE 5/5 Notificação: PULADA (--no-notify).")
        return "pulada", False

    if dry_run:
        log.info("FASE 5/5 Notificação (DRY-RUN): nada será enviado — abaixo o que sairia.")
        log.info(
            "DRY-RUN | canais ligados: telegram=%s, email=%s",
            settings.telegram_enabled, settings.email_enabled,
        )
        if settings.telegram_enabled:
            log.info("DRY-RUN | Telegram enviaria:\n%s", montar_mensagem_telegram(relatorio))
        if settings.email_enabled:
            log.info(
                "DRY-RUN | Email enviaria: assunto=%r | para=%s | corpo=relatorio.html (%d bytes)",
                _montar_assunto(relatorio), settings.email_to, len(html),
            )
        if not settings.telegram_enabled and not settings.email_enabled:
            log.info("DRY-RUN | nenhum canal ligado no .env.")
        return "dry-run", False

    # Envio real.
    log.info("FASE 5/5 Notificação: início")
    resultados = Dispatcher.from_settings(settings).enviar(relatorio, html)
    if not resultados:
        log.warning("FASE 5/5 Notificação: nenhum canal ligado no .env (nada enviado).")
        return resultados, False

    for canal, status in resultados.items():
        nivel = logging.INFO if status == "ok" else logging.ERROR
        log.log(nivel, "FASE 5/5 Notificação: canal %s -> %s", canal, status)

    falhou_tudo = all(status != "ok" for status in resultados.values())
    log.info("FASE 5/5 Notificação: fim | %s", resultados)
    return resultados, falhou_tudo


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def executar_pipeline(
    settings: Settings,
    *,
    csv: Optional[Path] = None,
    dry_run: bool = False,
    no_notify: bool = False,
) -> dict:
    """Roda o pipeline completo. Levanta apenas em falhas FATAIS (ingestão,
    análise, relatório). Falha de LLM é tratada como degradação, não erro."""
    destino = settings.output_dir
    destino.mkdir(parents=True, exist_ok=True)
    csv_path = csv or settings.input_csv

    # --- FASE 1: Ingestão ---------------------------------------------------
    log.info("FASE 1/5 Ingestão: início | csv=%s", csv_path)
    ingestao = ingerir(csv_path)
    r = ingestao.resumo
    log.info(
        "FASE 1/5 Ingestão: fim | %d válidas, %d incompletas, %d inválidas | "
        "limpezas: dedup=%d, gasto_br=%d, cliques_neg=%d",
        r["linhas_validas"], r["linhas_incompletas"], r["linhas_invalidas"],
        r["duplicatas_removidas"], r["gasto_formato_br_corrigido"],
        r["cliques_link_negativos_corrigidos"],
    )

    # --- FASE 2: Análise determinística ------------------------------------
    log.info("FASE 2/5 Análise: início")
    anomalias = analisar(ingestao.linhas)
    total_campanhas = len({linha.campanha_id for linha in ingestao.linhas})
    saida_fase2 = construir_saida(anomalias, total_campanhas)
    (destino / "anomalias.json").write_text(
        json.dumps(saida_fase2, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ps = saida_fase2["anomalias_por_severidade"]
    log.info(
        "FASE 2/5 Análise: fim | %d anomalias (%d críticas, %d altas, %d médias) "
        "em %d campanhas",
        saida_fase2["total_anomalias"], ps.get("crítica", 0), ps.get("alta", 0),
        ps.get("média", 0), total_campanhas,
    )

    # --- FASE 3: Camada LLM (NUNCA aborta o pipeline) ----------------------
    log.info("FASE 3/5 LLM: início | modelo=%s", settings.llm_model_diagnostico)
    try:
        diagnostico = gerar_diagnostico(saida_fase2, settings)
    except Exception as exc:  # noqa: BLE001 — contrato é degradar; cinto de segurança
        from src.llm import _resumo_deterministico

        log.error("FASE 3/5 LLM: exceção inesperada — seguindo DEGRADADO: %s", exc)
        diagnostico = {
            "llm_disponivel": False,
            "aviso": f"erro inesperado na camada LLM: {type(exc).__name__}: {exc}",
            "modelo": None,
            "resumo_executivo": _resumo_deterministico(saida_fase2),
            "campanhas": [],
        }
    (destino / "diagnostico.json").write_text(
        json.dumps(diagnostico, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if diagnostico.get("llm_disponivel"):
        log.info("FASE 3/5 LLM: fim | DISPONÍVEL (modelo=%s)", diagnostico.get("modelo"))
    else:
        log.warning("FASE 3/5 LLM: fim | DEGRADADO — %s", diagnostico.get("aviso"))

    # --- FASE 4: Relatório --------------------------------------------------
    log.info("FASE 4/5 Relatório: início")
    relatorio = construir_relatorio(saida_fase2, diagnostico, ingestao)
    html = renderizar_html(relatorio)
    (destino / "relatorio.json").write_text(
        json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (destino / "relatorio.html").write_text(html, encoding="utf-8")
    t = relatorio["totais"]
    log.info(
        "FASE 4/5 Relatório: fim | %d campanhas (%d com anomalia, %d estáveis) | "
        "relatorio.json + relatorio.html",
        t["campanhas"], t["campanhas_com_anomalia"], t["campanhas_estaveis"],
    )

    # --- FASE 5: Notificação ------------------------------------------------
    notificacao, notificacao_falhou_tudo = _etapa_notificacao(
        settings, relatorio, html, dry_run, no_notify
    )

    return {
        "linhas_validas": r["linhas_validas"],
        "total_anomalias": saida_fase2["total_anomalias"],
        "llm_disponivel": diagnostico.get("llm_disponivel", False),
        "notificacao": notificacao,
        "notificacao_falhou_tudo": notificacao_falhou_tudo,
    }


# --------------------------------------------------------------------------- #
# CLI / entrada
# --------------------------------------------------------------------------- #
def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Agente Analista de Campanhas — pipeline ponta a ponta.",
    )
    parser.add_argument("--csv", type=Path, default=None,
                        help="Sobrescreve o CSV de entrada (default: config/INPUT_CSV).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Roda tudo e gera o relatório, mas NÃO envia (loga o que sairia).")
    parser.add_argument("--no-notify", action="store_true",
                        help="Pula a etapa de notificação.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None, settings: Optional[Settings] = None) -> int:
    """Ponto de entrada. Retorna o exit code (0 sucesso, não-zero falha)."""
    args = _parse_args(argv)
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    log.info("==== Agente Analista de Campanhas: INÍCIO ====")
    try:
        resumo = executar_pipeline(
            settings, csv=args.csv, dry_run=args.dry_run, no_notify=args.no_notify
        )
    except IngestionError as exc:
        log.error("FALHA FATAL na ingestão do CSV: %s", exc)
        log.debug("Traceback:", exc_info=True)
        log.info("==== FIM: FALHA (exit 1) ====")
        return 1
    except Exception as exc:  # noqa: BLE001 — fronteira de topo: msg clara, sem stack cru
        log.error("FALHA FATAL inesperada (%s): %s", type(exc).__name__, exc)
        log.debug("Traceback:", exc_info=True)
        log.info("==== FIM: FALHA (exit 1) ====")
        return 1

    if resumo["notificacao_falhou_tudo"]:
        log.error(
            "==== FIM: relatório gerado, mas TODOS os canais de notificação "
            "falharam (exit 1) ===="
        )
        return 1

    log.info(
        "==== FIM: SUCESSO (exit 0) | %d linhas válidas, %d anomalias, LLM %s ====",
        resumo["linhas_validas"], resumo["total_anomalias"],
        "disponível" if resumo["llm_disponivel"] else "degradado",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
