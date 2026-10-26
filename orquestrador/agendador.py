"""
Orquestrador de Sprints de Ofertas — diquinhas & promos 🎀✨

Arquitetura de Controle de Janelas no CRON (Anti-Leak / Anti-Spam):
-------------------------------------------------------------------
No APScheduler (`trigger="cron"`), os campos `hour` e `minute` são avaliados como um
produto cartesiano independente (`hora IN H AND minuto IN M`). Por isso:

1. **Janelas Intra-Hora (início em `:00` e término em `:30` da mesma hora)**:
   - Ex.: Bloco 3 (13:00–13:30 a cada 6 min), Bloco 5 (18:00–18:30 a cada 10 min) e
     Bloco 6 (20:00–20:30 a cada 10 min).
   - Utiliza-se `hour=H` fixo combinado com intervalo fechado e passo `minute="0-30/6"`
     (que expande exatamente para `0,6,12,18,24,30`) ou `minute="0-30/10"` (`0,10,20,30`).
   - Jamais se utiliza `minute="*/6"` ou `minute="*/10"`, pois isso vazaria disparos para
     a segunda metade da hora (`:36, :40, :42, :48, :50, :54`).

2. **Janelas Trans-Hora (início em `:30` da hora `H` e término cravado em `:00` da hora `H+1`)**:
   - Ex.: Bloco 1 (08:30–09:00), Bloco 2 (11:30–12:00), Bloco 4 (15:30–16:00) e
     Bloco 7 (21:30–22:00).
   - Se configurássemos um único job com `hour="8-9", minute="0,30,40,50"`, o produto
     cartesiano dispararia indevidamente às `08:00` (antes da janela) e às `09:30, 09:40, 09:50`
     (vazando após as `09:00`).
   - Para impedir qualquer vazamento fora da janela:
     a) O job principal (`_p1`) fixa `hour=H` e restringe `minute` ao intervalo `30-59/passo`
        (ex.: `hour=8, minute="30-50/10"` -> `08:30, 08:40, 08:50`).
     b) O job de fechamento de janela (`_p2`) fixa `hour=H+1, minute="0"` (ex.: `09:00`),
        executando exclusivamente o último disparo do Sprint e encerrando o bloco.
"""

from collections import defaultdict
import logging
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database.historico_ofertas import HistoricoDB
from extractors.mercadolivre import (
    ML_TOPIC_CONTAINERS,
    TOPIC_EMOJIS,
    MercadoLivreExtractor,
    ProdutoOferta,
)
from extractors.shopee import ShopeeExtractor
from formatters.promo_formatter import PromoFormatter
from publishers.telegram_publisher import publish_product_to_telegram

logger = logging.getLogger("diquinhas_e_promos.agendador")

TIMEZONE = "America/Sao_Paulo"
BASE_DIR = Path(__file__).resolve().parent.parent
SHOPEE_CSV_DIR = BASE_DIR / "data" / "shopee"

# Instância global do AsyncIOScheduler configurada no fuso America/Sao_Paulo
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Instância global do banco SQLite de histórico (janela de deduplicação de 14 dias)
db = HistoricoDB()

# Contadores de rotação para variar os produtos a cada disparo dentro do mesmo Sprint
_sprint_counters: dict[str, int] = defaultdict(int)

# Pool de buscas rotativas por nicho para evitar repetição em disparos consecutivos
NICHO_ROTATION_QUERIES: dict[str, list[str]] = {
    "rosto_e_corpo": [
        "Base Liquida Payot",
        "Gloss Labial Payot",
        "Gel Limpeza Facial Monange",
        "Agua Micelar L Oreal",
    ],
    "unhas": [
        "Esmalte Risque",
        "Esmalte Impala",
        "Base Fortalecedora Unhas Risque",
    ],
    "moda": [
        "Vestido Feminino Midi",
        "Vestido Longo Festa",
        "Vestido Curto Feminino",
    ],
    "cabelo": [
        "Mascara Capilar Wella",
        "Condicionador Elseve Oleo Extraordinario",
        "Coloracao Amend Magnific",
        "Coloracao Haskell Excllusiv",
    ],
    "k-beauty": [
        "Serum Facial Dermachem",
        "Protetor Solar Facial",
        "Mascara Facial Coreana",
        "Hidratante Facial",
    ],
}

NICHO_TO_CATEGORY: dict[str, str] = {
    "rosto e corpo": "rosto_e_corpo",
    "rosto_e_corpo": "rosto_e_corpo",
    "unhas": "unhas",
    "moda": "moda",
    "cabelo": "cabelo",
    "k-beauty": "k-beauty",
    "kbeauty": "k-beauty",
    "beleza premium": "beleza_premium",
    "beleza_premium": "beleza_premium",
}


async def disparar_oferta(nicho: str) -> bool:
    """
    Função assíncrona base acionada pelos gatilhos CRON do APScheduler.
    Busca uma lista de até 20 produtos do nicho correspondente, percorre a lista
    verificando no SQLite (`HistoricoDB`) qual é o primeiro produto inédito nos
    últimos 14 dias, publica no Telegram, registra no banco e encerra com `break`.
    """
    logger.info("Extraindo 1 produto de %s", nicho)

    categoria = NICHO_TO_CATEGORY.get(nicho.strip().lower(), nicho.strip().lower())
    idx = _sprint_counters[categoria]
    _sprint_counters[categoria] += 1

    formatter = PromoFormatter()

    try:
        if categoria == "beleza_premium" and SHOPEE_CSV_DIR.exists():
            shopee_ext = ShopeeExtractor()
            raw_dicts = shopee_ext.extrair_de_pastas_csv(
                base_dir=SHOPEE_CSV_DIR,
                por_marca=4,
                categoria="beleza_premium",
                emojis="💎✨💄",
            )
            produtos: list[ProdutoOferta] = [ProdutoOferta(**d) for d in raw_dicts]
        else:
            ml_ext = MercadoLivreExtractor()
            queries = NICHO_ROTATION_QUERIES.get(categoria, [nicho])
            query_escolhida = queries[idx % len(queries)]
            emojis = TOPIC_EMOJIS.get(categoria, "✨💖🔥")

            produtos = ml_ext.buscar_ofertas(
                query=query_escolhida,
                categoria=categoria,
                emojis=emojis,
                limit=20,
            )

        if not produtos:
            logger.warning("Nenhum produto encontrado para o nicho '%s'.", nicho)
            return False

        containers = ML_TOPIC_CONTAINERS.get(categoria)

        publicado = False
        for produto in produtos:
            if not db.foi_postado_recentemente(produto.id):
                dados_produto = produto.model_dump()
                if containers and categoria != "beleza_premium":
                    dados_produto["link_produto"] = containers[idx % len(containers)]

                oferta_afiliada = formatter.aplicar_afiliado(dados_produto)
                sucesso = await publish_product_to_telegram(oferta_afiliada)
                if sucesso:
                    db.registrar_postagem(produto.id, nicho)
                    publicado = True
                break
            else:
                logger.info(
                    "Produto '%s' (%s) já postado nos últimos 14 dias; buscando próximo da lista...",
                    produto.id,
                    produto.titulo[:40],
                )

        if not publicado:
            logger.warning(
                "Nenhum produto inédito pôde ser publicado para o nicho '%s' neste ciclo.",
                nicho,
            )
        return publicado
    except Exception as exc:
        logger.error("Erro ao disparar oferta do nicho '%s': %s", nicho, exc)
        return False


def configurar_agendador(sched: AsyncIOScheduler | None = None) -> AsyncIOScheduler:
    """
    Registra rigorosamente os 7 Blocos (Sprints) no AsyncIOScheduler usando o gatilho `cron`
    no fuso horário `America/Sao_Paulo`, garantindo que nenhum job vaze fora das janelas.
    """
    target_scheduler = sched or scheduler

    # =========================================================================
    # BLOCO 1 (Rosto e Corpo): das 08:30 às 09:00, a cada 10 minutos
    # Disparos exatos: 08:30, 08:40, 08:50 e 09:00
    # =========================================================================
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=8,
        minute="30-50/10",
        args=["Rosto e Corpo"],
        id="bloco_1_rosto_e_corpo_08h",
        name="BLOCO 1 (Rosto e Corpo) - 08:30 às 08:50",
        replace_existing=True,
        misfire_grace_time=120,
    )
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=9,
        minute="0",
        args=["Rosto e Corpo"],
        id="bloco_1_rosto_e_corpo_09h00",
        name="BLOCO 1 (Rosto e Corpo) - Fechamento 09:00",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # =========================================================================
    # BLOCO 2 (Unhas): das 11:30 às 12:00, a cada 15 minutos
    # Disparos exatos: 11:30, 11:45 e 12:00
    # =========================================================================
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=11,
        minute="30-45/15",
        args=["Unhas"],
        id="bloco_2_unhas_11h",
        name="BLOCO 2 (Unhas) - 11:30 às 11:45",
        replace_existing=True,
        misfire_grace_time=120,
    )
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=12,
        minute="0",
        args=["Unhas"],
        id="bloco_2_unhas_12h00",
        name="BLOCO 2 (Unhas) - Fechamento 12:00",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # =========================================================================
    # BLOCO 3 (Moda): das 13:00 às 13:30, a cada 6 minutos
    # Disparos exatos: 13:00, 13:06, 13:12, 13:18, 13:24 e 13:30
    # =========================================================================
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=13,
        minute="0-30/6",
        args=["Moda"],
        id="bloco_3_moda_13h",
        name="BLOCO 3 (Moda) - 13:00 às 13:30 (a cada 6 min)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # =========================================================================
    # BLOCO 4 (Cabelo): das 15:30 às 16:00, a cada 10 minutos
    # Disparos exatos: 15:30, 15:40, 15:50 e 16:00
    # =========================================================================
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=15,
        minute="30-50/10",
        args=["Cabelo"],
        id="bloco_4_cabelo_15h",
        name="BLOCO 4 (Cabelo) - 15:30 às 15:50",
        replace_existing=True,
        misfire_grace_time=120,
    )
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=16,
        minute="0",
        args=["Cabelo"],
        id="bloco_4_cabelo_16h00",
        name="BLOCO 4 (Cabelo) - Fechamento 16:00",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # =========================================================================
    # BLOCO 5 (Moda): das 18:00 às 18:30, a cada 10 minutos
    # Disparos exatos: 18:00, 18:10, 18:20 e 18:30
    # =========================================================================
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=18,
        minute="0-30/10",
        args=["Moda"],
        id="bloco_5_moda_18h",
        name="BLOCO 5 (Moda) - 18:00 às 18:30 (a cada 10 min)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # =========================================================================
    # BLOCO 6 (K-Beauty): das 20:00 às 20:30, a cada 10 minutos
    # Disparos exatos: 20:00, 20:10, 20:20 e 20:30
    # =========================================================================
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=20,
        minute="0-30/10",
        args=["K-Beauty"],
        id="bloco_6_kbeauty_20h",
        name="BLOCO 6 (K-Beauty) - 20:00 às 20:30 (a cada 10 min)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # =========================================================================
    # BLOCO 7 (Rosto e Corpo): das 21:30 às 22:00, a cada 6 minutos
    # Disparos exatos: 21:30, 21:36, 21:42, 21:48, 21:54 e 22:00
    # =========================================================================
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=21,
        minute="30-54/6",
        args=["Rosto e Corpo"],
        id="bloco_7_rosto_e_corpo_21h",
        name="BLOCO 7 (Rosto e Corpo) - 21:30 às 21:54",
        replace_existing=True,
        misfire_grace_time=120,
    )
    target_scheduler.add_job(
        disparar_oferta,
        trigger="cron",
        hour=22,
        minute="0",
        args=["Rosto e Corpo"],
        id="bloco_7_rosto_e_corpo_22h00",
        name="BLOCO 7 (Rosto e Corpo) - Fechamento 22:00",
        replace_existing=True,
        misfire_grace_time=120,
    )

    logger.info(
        "Agendador configurado com %d jobs CRON no fuso '%s'.",
        len(target_scheduler.get_jobs()),
        TIMEZONE,
    )
    return target_scheduler
