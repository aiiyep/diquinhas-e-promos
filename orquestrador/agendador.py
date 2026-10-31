"""
Orquestrador de Sprints de Ofertas — diquinhas & promos 🎀✨

Arquitetura de Controle de Janelas no CRON (Anti-Leak / Anti-Spam):
-------------------------------------------------------------------
No APScheduler (`trigger="cron"`), os campos `hour` e `minute` são avaliados como um
produto cartesiano independente (`hora IN H AND minuto IN M`). Por isso:

1. **Janelas Intra-Hora (início em `:00` e término em `:30` da mesma hora)**:
   - Utiliza-se `hour=H` fixo combinado com intervalo fechado e passo `minute="0-30/6"`
     (que expande exatamente para `0,6,12,18,24,30`) ou `minute="0-30/10"` (`0,10,20,30`).
   - Jamais se utiliza `minute="*/6"` ou `minute="*/10"`, pois isso vazaria disparos para
     a segunda metade da hora (`:36, :40, :42, :48, :50, :54`).

2. **Janelas Trans-Hora (início em `:30` da hora `H` e término cravado em `:00` da hora `H+1`)**:
   - Um único job com `hour="8-9", minute="0,30,40,50"` dispararia indevidamente às `08:00`
     e às `09:30, 09:40, 09:50`. Por isso cada janela vira dois jobs:
     a) principal: `hour=H, minute="30-50/10"` -> `08:30, 08:40, 08:50`;
     b) fechamento: `hour=H+1, minute="0"` -> `09:00`.

Os blocos ficam declarados na tabela `BLOCOS` e são registrados por `configurar_agendador`.
"""

import asyncio
from collections import defaultdict
import logging
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database.historico_ofertas import HistoricoDB
from extractors.mercadolivre import (
    OFERTAS_SUBCATEGORIAS,
    PERFUMES_CATEGORIA,
    PERFUMES_DOMINIOS,
    PERFUMES_FEMININOS_TERMOS,
    TOPIC_EMOJIS,
    MercadoLivreExtractor,
)
from extractors.filtros import eh_produto_masculino
from extractors.shopee import ShopeeExtractor
from formatters.promo_formatter import PromoFormatter
from publishers.telegram_publisher import publish_product_to_telegram

logger = logging.getLogger("diquinhas_e_promos.agendador")

TIMEZONE = "America/Sao_Paulo"
BASE_DIR = Path(__file__).resolve().parent.parent
SHOPEE_CSV_DIR = BASE_DIR / "data" / "shopee"
# CSVs da Shopee Afiliados do K-Beauty: uma subpasta por loja (glory-of-seoul, jadoo, ...)
KBEAUTY_CSV_DIR = BASE_DIR / "data" / "kbeauty"

# CSVs da Shopee Afiliados do tópico Ofertas (soltos na pasta ou em subpastas)
OFERTAS_CSV_DIR = BASE_DIR / "data" / "ofertas"

# Como cada fonte de CSV da Shopee é lida: pasta, emojis, quantos produtos por loja/marca,
# se alterna as lojas a cada disparo e se só aceita produto com desconto real informado no CSV
FONTES_SHOPEE: dict[str, dict[str, Any]] = {
    "beleza_premium": {"pasta": SHOPEE_CSV_DIR, "emojis": "💎✨💄", "por_marca": 10,
                       "intercalar": False, "so_com_desconto": False},
    # K-Beauty usa o catálogo inteiro de cada loja e alterna a loja a cada disparo
    "k-beauty": {"pasta": KBEAUTY_CSV_DIR, "emojis": TOPIC_EMOJIS["k-beauty"], "por_marca": 1000,
                 "intercalar": True, "so_com_desconto": False},
}
# Metade Shopee do tópico Ofertas: mesmo critério do ML (desconto real, maior desconto primeiro)
OFERTAS_SHOPEE: dict[str, Any] = {"pasta": OFERTAS_CSV_DIR, "emojis": TOPIC_EMOJIS["ofertas"], "por_marca": 1000,
                                  "intercalar": False, "so_com_desconto": True}

# Quantos envios ao Telegram podem falhar seguidos antes de desistir do disparo
MAX_FALHAS_ENVIO = 3

# Instância global do AsyncIOScheduler configurada no fuso America/Sao_Paulo
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Instância global do banco SQLite de histórico (janela de deduplicação de 14 dias)
db = HistoricoDB()

# Contadores de rotação para variar os produtos a cada disparo dentro do mesmo Sprint
_sprint_counters: dict[str, int] = defaultdict(int)

# Palavras-chave inspiradas nas vitrines (containers) do Mercado Livre enviadas pelo usuário.
# As páginas _Container_ bloqueiam acesso automatizado (captcha), então cada tópico usa marcas
# e produtos alinhados ao tema da vitrine, validados no catálogo oficial do ML (só entram termos
# que retornam produtos com anúncio ativo). Combinado com paginação automática (páginas 0, 1, 2).
NICHO_ROTATION_QUERIES: dict[str, list[str]] = {
    # Vitrines: makes-virais-face, makes-virais-labios e viralizados-skin-care
    "rosto_e_corpo": [
        "Base Niina Secrets",
        "Gloss Fran Liphoney",
        "Serum Sallve",
        "Primer Vizzela",
        "Gloss Kiko Milano 3D Hydra",
        "Biore Cleansing Oil",
        "Blush Boca Rosa",
        "Lip Tint Ruby Rose",
        "Serum Niacinamida Principia",
        "Base Fran by Franciny Ehlke",
        "Gloss Mia Make",
        "Protetor Solar Episol",
        "Po Banana Max Love",
        "Hidra Labios Boca Rosa",
        "Cicaplast Baume B5 La Roche Posay",
        "Corretivo Boca Rosa",
        "Gloss Max Love",
        "Protetor Solar Anthelios",
        "Iluminador Melu",
        "Lapis Labial Mari Maria",
        "Gel Limpeza Neutrogena",
        "Base Vizzela",
        "Gloss Labial Oceane",
        "Protetor Solar Sallve",
        "Po Solto Vizzela",
    ],
    # Vitrine: beauty-manicure-e-pedicure
    "unhas": [
        "Esmalte Risque",
        "Alicate de Cuticula Mundial",
        "Esmalte Dailus",
        "Removedor de Esmalte",
        "Esmalte Impala",
        "Fortalecedor Unhas Granado",
        "Esmalte Ludurana",
        "Lixa de Unha",
        "Oleo Secante",
        "Esmalte Colorama",
        "Esmalte em Gel",
        "Esmalte Anita",
        "Amolecedor de Cuticula",
        "Kit Unha Gel",
        "Unha Postica",
    ],
    # Vitrine: moda-fashion (feminino)
    "moda": [
        "Calca Pantalona Feminina",
        "Vestido Midi Feminino",
        "Calca Wide Leg Feminina",
        "Sandalia Feminina",
        "Calca Jeans Feminina",
        "Cropped Feminino",
        "Cardigan Feminino",
        "Short Feminino",
        "Biquini",
        "Oculos de Sol Feminino",
        "Pijama Feminino",
    ],
    # Vitrine: hair-beauty
    "cabelo": [
        "Mascara Wella Oil Reflections",
        "Elseve Cicatri Renov",
        "Lola Morte Subita",
        "Wella Fusion",
        "Inoar Mascara",
        "Lola Cosmetics Mascara",
        "Wella Invigo",
        "Haskell Mascara",
        "Leave In Lola",
        "Elseve Oleo Extraordinario",
        "Amend Mascara",
        "Elseve Glycolic Gloss",
        "Oleo Capilar Pantene",
        "Salon Line Todecacho",
        "Creme de Pentear Seda",
        "Shampoo Antiresiduo",
        "Kerastase Mascara",
        "Loreal Professionnel Absolut Repair",
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
    "ofertas": "ofertas",
}

# Tabela dos Sprints: cada linha vira um job CRON (ver regras anti-vazamento no topo do arquivo)
BLOCOS: list[dict[str, Any]] = [
    # BLOCO 0 (Ofertas): mais vendidos de Beleza com desconto real — 10:00, 10:10, 10:20, 10:30
    {"id": "bloco_0_ofertas_10h", "nicho": "Ofertas", "hour": 10, "minute": "0-30/10",
     "name": "BLOCO 0 (Ofertas) - 10:00 às 10:30 (a cada 10 min)"},
    # BLOCO 1 (Rosto e Corpo): 08:30, 08:40, 08:50 + fechamento 09:00
    {"id": "bloco_1_rosto_e_corpo_08h", "nicho": "Rosto e Corpo", "hour": 8, "minute": "30-50/10",
     "name": "BLOCO 1 (Rosto e Corpo) - 08:30 às 08:50"},
    {"id": "bloco_1_rosto_e_corpo_09h00", "nicho": "Rosto e Corpo", "hour": 9, "minute": "0",
     "name": "BLOCO 1 (Rosto e Corpo) - Fechamento 09:00"},
    # BLOCO 2 (Unhas): 11:30, 11:45 + fechamento 12:00
    {"id": "bloco_2_unhas_11h", "nicho": "Unhas", "hour": 11, "minute": "30-45/15",
     "name": "BLOCO 2 (Unhas) - 11:30 às 11:45"},
    {"id": "bloco_2_unhas_12h00", "nicho": "Unhas", "hour": 12, "minute": "0",
     "name": "BLOCO 2 (Unhas) - Fechamento 12:00"},
    # BLOCO 3 (Moda): 13:00, 13:06, 13:12, 13:18, 13:24, 13:30
    {"id": "bloco_3_moda_13h", "nicho": "Moda", "hour": 13, "minute": "0-30/6",
     "name": "BLOCO 3 (Moda) - 13:00 às 13:30 (a cada 6 min)"},
    # BLOCO 4 (Cabelo): 15:30, 15:40, 15:50 + fechamento 16:00
    {"id": "bloco_4_cabelo_15h", "nicho": "Cabelo", "hour": 15, "minute": "30-50/10",
     "name": "BLOCO 4 (Cabelo) - 15:30 às 15:50"},
    {"id": "bloco_4_cabelo_16h00", "nicho": "Cabelo", "hour": 16, "minute": "0",
     "name": "BLOCO 4 (Cabelo) - Fechamento 16:00"},
    # BLOCO 5 (Moda): 18:00, 18:10, 18:20, 18:30
    {"id": "bloco_5_moda_18h", "nicho": "Moda", "hour": 18, "minute": "0-30/10",
     "name": "BLOCO 5 (Moda) - 18:00 às 18:30 (a cada 10 min)"},
    # BLOCO 6 (K-Beauty / Shopee CSV): 19:00, 19:10, 19:20, 19:30
    {"id": "bloco_6_kbeauty_19h", "nicho": "K-Beauty", "hour": 19, "minute": "0-30/10",
     "name": "BLOCO 6 (K-Beauty) - 19:00 às 19:30 (a cada 10 min)"},
    # BLOCO 6 (Beleza Premium / Shopee CSV): 20:00, 20:10, 20:20, 20:30
    {"id": "bloco_6_beleza_premium_20h", "nicho": "Beleza Premium", "hour": 20, "minute": "0-30/10",
     "name": "BLOCO 6 (Beleza Premium) - 20:00 às 20:30 (a cada 10 min)"},
    # BLOCO 7 (Rosto e Corpo): 21:30, 21:36, 21:42, 21:48, 21:54 + fechamento 22:00
    {"id": "bloco_7_rosto_e_corpo_21h", "nicho": "Rosto e Corpo", "hour": 21, "minute": "30-54/6",
     "name": "BLOCO 7 (Rosto e Corpo) - 21:30 às 21:54"},
    {"id": "bloco_7_rosto_e_corpo_22h00", "nicho": "Rosto e Corpo", "hour": 22, "minute": "0",
     "name": "BLOCO 7 (Rosto e Corpo) - Fechamento 22:00"},
]


class _ControleEnvio:
    """Publica o primeiro produto inédito e tolera até `MAX_FALHAS_ENVIO` falhas seguidas."""

    def __init__(self, nicho: str) -> None:
        self.nicho = nicho
        self.falhas = 0
        self.publicado: Optional[str] = None
        self.formatter = PromoFormatter()

    @property
    def esgotado(self) -> bool:
        return self.falhas >= MAX_FALHAS_ENVIO

    def resumo(self) -> str:
        """Texto curto do resultado, gravado no histórico de disparos do painel."""
        if self.publicado:
            return self.publicado
        if self.esgotado:
            return f"{self.falhas} envios ao Telegram falharam seguidos"
        return "Nenhum produto inédito encontrado"

    async def tentar(self, produto_id: str, dados_produto: dict[str, Any]) -> bool:
        oferta_afiliada = self.formatter.aplicar_afiliado(dados_produto)
        if await publish_product_to_telegram(oferta_afiliada):
            db.registrar_postagem(
                produto_id,
                self.nicho,
                titulo=oferta_afiliada.get("titulo"),
                preco=oferta_afiliada.get("preco_promocional"),
                link=oferta_afiliada.get("link_produto"),
                loja=oferta_afiliada.get("loja"),
            )
            self.publicado = str(oferta_afiliada.get("titulo") or produto_id)
            return True
        self.falhas += 1
        logger.warning(
            "Envio de '%s' falhou (%d/%d); tentando o próximo produto.",
            produto_id,
            self.falhas,
            MAX_FALHAS_ENVIO,
        )
        return False


def _intercalar_por_marca(candidatos: list[dict[str, Any]], inicio: int) -> list[dict[str, Any]]:
    """
    Reordena os candidatos alternando entre as lojas/marcas (A1, B1, C1, A2, B2...), mantendo a
    ordem de vendas dentro de cada uma. `inicio` escolhe a loja que abre a fila, para que
    disparos seguidos comecem por lojas diferentes.
    """
    por_marca: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cand in candidatos:
        por_marca[cand.get("marca", "")].append(cand)
    marcas = list(por_marca)
    if not marcas:
        return []
    marcas = marcas[inicio % len(marcas):] + marcas[: inicio % len(marcas)]
    filas = [por_marca[m] for m in marcas]
    intercalados: list[dict[str, Any]] = []
    for posicao in range(max(len(f) for f in filas)):
        intercalados.extend(f[posicao] for f in filas if posicao < len(f))
    return intercalados


def _desconto(dados: dict[str, Any]) -> float:
    """Fração de desconto real (0.25 = 25%); 0 quando o CSV não informa o preço "De"."""
    original = dados.get("preco_original")
    return 1 - dados["preco_promocional"] / original if original else 0.0


async def _disparar_shopee(
    nicho: str, categoria: str, idx: int, controle: _ControleEnvio, fonte: Optional[dict[str, Any]] = None
) -> bool:
    fonte = fonte or FONTES_SHOPEE[categoria]
    if not fonte["pasta"].exists():
        logger.warning("Pasta de CSVs da Shopee não encontrada em %s.", fonte["pasta"])
        return False
    shopee_ext = ShopeeExtractor()
    candidatos = await asyncio.to_thread(
        shopee_ext.extrair_de_pastas_csv,
        base_dir=fonte["pasta"],
        por_marca=fonte["por_marca"],
        categoria=categoria,
        emojis=fonte["emojis"],
        resolver_imagens=False,
    )
    if fonte["so_com_desconto"]:
        candidatos = sorted((c for c in candidatos if _desconto(c) > 0), key=_desconto, reverse=True)
    if fonte["intercalar"]:
        candidatos = _intercalar_por_marca(candidatos, idx)
    for dados in candidatos:
        if controle.esgotado:
            break
        if db.foi_postado_recentemente(dados["id"]):
            continue
        if eh_produto_masculino(dados["titulo"]):
            logger.info("Produto '%s' ignorado por ser masculino.", dados["titulo"][:40])
            continue
        if not dados.get("imagem_url"):
            dados["imagem_url"] = await asyncio.to_thread(
                shopee_ext.buscar_imagem_oficial, dados["link_produto"]
            )
        if await controle.tentar(dados["id"], dados):
            return True
    logger.warning("Nenhum produto inédito de '%s' pôde ser publicado.", nicho)
    return False


async def _disparar_ofertas(nicho: str, idx: int, controle: _ControleEnvio) -> bool:
    """
    Alterna Mercado Livre e Shopee a cada disparo (par = ML, ímpar = Shopee). Se a loja da vez
    não tiver nada inédito com desconto real, tenta a outra.
    """
    ordem = [_disparar_ofertas_ml, _disparar_ofertas_shopee]
    if idx % 2:
        ordem.reverse()
    for fonte in ordem:
        if controle.esgotado:
            return False
        if await fonte(nicho, idx // 2, controle):
            return True
    return False


async def _disparar_ofertas_shopee(nicho: str, idx: int, controle: _ControleEnvio) -> bool:
    """Produto dos CSVs de `data/ofertas/` com o maior desconto real informado, sem masculinos."""
    return await _disparar_shopee(nicho, "ofertas", idx, controle, fonte=OFERTAS_SHOPEE)


async def _disparar_ofertas_ml(nicho: str, idx: int, controle: _ControleEnvio) -> bool:
    """
    Publica o mais vendido de Beleza com maior desconto real ainda não postado, rodando
    entre as subcategorias femininas (`OFERTAS_SUBCATEGORIAS`) a cada disparo.
    Em Perfumes, busca 3 perfumes femininos conhecidos por vez (`PERFUMES_FEMININOS_TERMOS`).
    """
    ml_ext = MercadoLivreExtractor()
    subcategorias = list(OFERTAS_SUBCATEGORIAS)
    opcoes = {"categoria": "ofertas", "emojis": TOPIC_EMOJIS["ofertas"], "ignorar": db.foi_postado_recentemente}

    for offset in range(len(subcategorias)):
        categoria_id = subcategorias[(idx + offset) % len(subcategorias)]
        logger.info("Buscando ofertas em %s (%s)...", OFERTAS_SUBCATEGORIAS[categoria_id], categoria_id)
        if categoria_id == PERFUMES_CATEGORIA:
            inicio = idx * 3
            termos = [PERFUMES_FEMININOS_TERMOS[(inicio + i) % len(PERFUMES_FEMININOS_TERMOS)] for i in range(3)]
            ofertas = await asyncio.to_thread(
                ml_ext.buscar_ofertas_por_termos, termos, dominios=PERFUMES_DOMINIOS, **opcoes
            )
        else:
            ofertas = await asyncio.to_thread(ml_ext.buscar_ofertas_mais_vendidos, categoria_id, **opcoes)
        for oferta in ofertas:
            if controle.esgotado:
                return False
            if await controle.tentar(oferta.id, oferta.model_dump()):
                return True

    logger.warning("Nenhuma oferta inédita com desconto real encontrada para '%s'.", nicho)
    return False


async def _disparar_mercado_livre(nicho: str, categoria: str, idx: int, controle: _ControleEnvio) -> bool:
    ml_ext = MercadoLivreExtractor()
    queries = NICHO_ROTATION_QUERIES.get(categoria, [nicho])
    emojis = TOPIC_EMOJIS.get(categoria, "✨💖🔥")

    # Percorre as subcategorias rotativas e avança páginas automaticamente (pág 0, 1, 2)
    # caso os primeiros 20 resultados da subcategoria já tenham sido postados nos últimos 14 dias.
    max_queries_to_try = min(len(queries), 4)
    max_pages_per_query = 3

    for q_offset in range(max_queries_to_try):
        query_escolhida = queries[(idx + q_offset) % len(queries)]
        for page in range(max_pages_per_query):
            try:
                produtos = await asyncio.to_thread(
                    ml_ext.buscar_ofertas,
                    query=query_escolhida,
                    categoria=categoria,
                    emojis=emojis,
                    limit=20,
                    page=page,
                )
            except ValueError:
                break

            if not produtos:
                break

            for produto in produtos:
                if controle.esgotado:
                    return False
                if db.foi_postado_recentemente(produto.id):
                    logger.info(
                        "Produto '%s' (%s) já postado nos últimos 14 dias; buscando próximo da lista...",
                        produto.id,
                        produto.titulo[:40],
                    )
                    continue

                dados_produto = produto.model_dump()
                # Resolve o link direto da página do produto no Mercado Livre (sem sobrescrever com o container)
                dados_produto["link_produto"] = await asyncio.to_thread(
                    ml_ext.resolver_link_direto_produto,
                    titulo=produto.titulo,
                    fallback_url=produto.link_produto,
                )
                if await controle.tentar(produto.id, dados_produto):
                    return True

    logger.warning(
        "Nenhum produto inédito pôde ser publicado para o nicho '%s' neste ciclo.",
        nicho,
    )
    return False


async def disparar_oferta(nicho: str) -> bool:
    """
    Função assíncrona base acionada pelos gatilhos CRON do APScheduler.
    Busca os produtos do nicho, publica o primeiro inédito nos últimos 14 dias
    (consultando o `HistoricoDB`) e registra no banco. Se um envio falhar, tenta o
    próximo produto. As chamadas HTTP bloqueantes rodam em threads para não travar o loop.
    """
    logger.info("Extraindo 1 produto de %s", nicho)

    categoria = NICHO_TO_CATEGORY.get(nicho.strip().lower(), nicho.strip().lower())

    # Combina o contador em memória com o total já salvo no SQLite para manter a rotação mesmo após reboot do servidor
    idx = db.contar_postagens_nicho(nicho) + _sprint_counters[categoria]
    _sprint_counters[categoria] += 1

    controle = _ControleEnvio(nicho)
    try:
        if categoria in FONTES_SHOPEE:
            sucesso = await _disparar_shopee(nicho, categoria, idx, controle)
        elif categoria == "ofertas":
            sucesso = await _disparar_ofertas(nicho, idx, controle)
        else:
            sucesso = await _disparar_mercado_livre(nicho, categoria, idx, controle)
        db.registrar_disparo(nicho, sucesso, controle.resumo())
        return sucesso
    except Exception as exc:
        logger.error("Erro ao disparar oferta do nicho '%s': %s", nicho, exc)
        db.registrar_disparo(nicho, False, f"Erro: {exc}")
        return False


def configurar_agendador(sched: Optional[AsyncIOScheduler] = None) -> AsyncIOScheduler:
    """
    Registra os Sprints da tabela `BLOCOS` no AsyncIOScheduler usando o gatilho `cron`
    no fuso horário `America/Sao_Paulo`, garantindo que nenhum job vaze fora das janelas.
    """
    target_scheduler = sched or scheduler

    for bloco in BLOCOS:
        target_scheduler.add_job(
            disparar_oferta,
            trigger="cron",
            hour=bloco["hour"],
            minute=bloco["minute"],
            args=[bloco["nicho"]],
            id=bloco["id"],
            name=bloco["name"],
            replace_existing=True,
            misfire_grace_time=120,
        )

    logger.info(
        "Agendador configurado com %d jobs CRON no fuso '%s'.",
        len(target_scheduler.get_jobs()),
        TIMEZONE,
    )
    return target_scheduler
