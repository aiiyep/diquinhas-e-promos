import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import config  # noqa: F401  (carrega o .env antes dos demais módulos)
from extractors.mercadolivre import MercadoLivreExtractor
from extractors.shopee import ShopeeExtractor
from formatters.promo_formatter import PromoFormatter
from orquestrador.agendador import configurar_agendador, disparar_oferta, scheduler
from publishers.telegram_publisher import _resolve_thread_id, fechar_bot, publish_product_to_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("diquinhas_e_promos")

BASE_DIR = Path(__file__).resolve().parent
SHOPEE_CSV_DIR = BASE_DIR / "data" / "shopee"


async def run_telegram_pipeline(
    post_shopee_csvs: bool = True,
    post_ml_containers: bool = True,
    shopee_por_marca: int = 1,
) -> None:
    """
    Execução imediata (one-shot) de validação:
    1. Extrai produtos dos .csv da Shopee e publica no tópico 'beleza_premium'.
    2. Extrai produtos dos containers do Mercado Livre e publica nos respectivos tópicos.
    """
    formatter = PromoFormatter()

    if post_shopee_csvs and SHOPEE_CSV_DIR.exists():
        shopee_extractor = ShopeeExtractor()
        ofertas_shopee: list[dict[str, Any]] = await asyncio.to_thread(
            shopee_extractor.extrair_de_pastas_csv,
            base_dir=SHOPEE_CSV_DIR,
            por_marca=shopee_por_marca,
            categoria="beleza_premium",
            emojis="💎✨💄",
        )
        for oferta in ofertas_shopee:
            oferta_afiliada = formatter.aplicar_afiliado(oferta)
            await publish_product_to_telegram(oferta_afiliada)
            await asyncio.sleep(2)

    if post_ml_containers:
        ml_extractor = MercadoLivreExtractor()
        ofertas_ml: list[dict[str, Any]] = await asyncio.to_thread(
            ml_extractor.extrair_todos_containers_mapeados
        )
        for oferta in ofertas_ml:
            categoria = oferta.get("categoria", "")
            if _resolve_thread_id(categoria) is None:
                logger.warning(
                    "Tópico '%s' ainda sem THREAD_ID numérico no .env; pulando envio.",
                    categoria,
                )
                continue
            oferta_afiliada = formatter.aplicar_afiliado(oferta)
            await publish_product_to_telegram(oferta_afiliada)
            await asyncio.sleep(2)

    logger.info("Fluxo imediato finalizado com sucesso.")


async def iniciar_orquestrador_sprints(run_forever: bool = True) -> None:
    """
    Inicializa o AsyncIOScheduler com os 7 Blocos (Sprints) em America/Sao_Paulo,
    lista os próximos disparos agendados e mantém o event loop ativo.
    """
    configurar_agendador(scheduler)
    scheduler.start()

    logger.info("=== SPRINTS AGENDADOS (Fuso: America/Sao_Paulo) ===")
    for job in scheduler.get_jobs():
        logger.info(
            "Job [%s] | %s | Próximo disparo: %s",
            job.id,
            job.name,
            job.next_run_time,
        )

    if not run_forever:
        scheduler.shutdown(wait=False)
        return

    logger.info("Orquestrador em execução contínua. Pressione Ctrl+C para encerrar.")
    stop_event = asyncio.Event()
    await stop_event.wait()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de Ofertas — diquinhas & promos 🎀✨")
    parser.add_argument(
        "--check-schedule",
        action="store_true",
        help="Apenas inicializa e exibe a tabela de próximos disparos dos 7 blocos e encerra.",
    )
    parser.add_argument(
        "--test-nicho",
        type=str,
        default=None,
        help="Executa um disparo imediato de teste via disparar_oferta(nicho) (ex: 'Moda').",
    )
    parser.add_argument(
        "--run-all-now",
        action="store_true",
        help="Dispara imediatamente um lote completo (Shopee CSV + Containers ML).",
    )
    args = parser.parse_args()

    try:
        if args.test_nicho:
            await disparar_oferta(args.test_nicho)
        elif args.run_all_now:
            await run_telegram_pipeline()
        else:
            await iniciar_orquestrador_sprints(run_forever=not args.check_schedule)
    finally:
        await fechar_bot()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot encerrado pelo usuário.")
