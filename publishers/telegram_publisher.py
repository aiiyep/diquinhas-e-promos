import logging
import os
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from extractors.base import Category
from formatters.promo_formatter import FormattedPromotion
from publishers.base import BasePublisher

load_dotenv()

logger = logging.getLogger("diquinhas_e_promos.telegram")


def _resolve_thread_id(category: str) -> Optional[int]:
    """Mapeia a categoria do produto para o message_thread_id configurado no .env."""
    thread_mapping: dict[str, Optional[str]] = {
        "rosto_e_corpo": os.getenv("THREAD_ID_ROSTO_E_CORPO") or os.getenv("THREAD_ID_SKINCARE"),
        "rosto e corpo": os.getenv("THREAD_ID_ROSTO_E_CORPO") or os.getenv("THREAD_ID_SKINCARE"),
        "cabelo": os.getenv("THREAD_ID_CABELO"),
        "unhas": os.getenv("THREAD_ID_UNHAS"),
        "moda": os.getenv("THREAD_ID_MODA"),
        "skincare": os.getenv("THREAD_ID_SKINCARE") or os.getenv("THREAD_ID_PROTETOR_SOLAR"),
        "protetor_solar": os.getenv("THREAD_ID_PROTETOR_SOLAR"),
        "protetor solar": os.getenv("THREAD_ID_PROTETOR_SOLAR"),
        "hidratante": os.getenv("THREAD_ID_HIDRATANTE"),
        "limpeza_facial": os.getenv("THREAD_ID_LIMPEZA_FACIAL"),
        "limpeza facial": os.getenv("THREAD_ID_LIMPEZA_FACIAL"),
        "seruns_e_acidos": os.getenv("THREAD_ID_SERUNS_E_ACIDOS"),
        "séruns e ácidos": os.getenv("THREAD_ID_SERUNS_E_ACIDOS"),
        "k-beauty": os.getenv("THREAD_ID_KBEAUTY"),
        "kbeauty": os.getenv("THREAD_ID_KBEAUTY"),
        "beleza_premium": os.getenv("THREAD_ID_BELEZA_PREMIUM"),
        "beleza premium": os.getenv("THREAD_ID_BELEZA_PREMIUM"),
        "ofertas": os.getenv("THREAD_ID_OFERTAS"),
        "tecnologia": os.getenv("THREAD_ID_TECNOLOGIA") or os.getenv("THREAD_ID_OFERTAS"),
    }
    raw_thread_id = thread_mapping.get(category.strip().lower())
    if raw_thread_id and raw_thread_id.strip().isdigit():
        thread_id = int(raw_thread_id.strip())
        return thread_id if thread_id > 0 else None
    return None


def _format_brl(value: Any) -> str:
    """Formata um float ou string numérica para o padrão monetário brasileiro (XX,XX)."""
    if isinstance(value, (int, float)):
        return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(value)


def _build_html_message(product: dict[str, Any]) -> str:
    """Monta a mensagem/legenda em HTML a partir do dicionário do produto (incluindo avaliação ⭐)."""
    emojis = product.get("emojis", "🔥")
    title = product.get("titulo", "Oferta Imperdível")

    raw_rating = product.get("avaliacao", product.get("rating", 4.9))
    try:
        rating_val = float(str(raw_rating).replace(",", "."))
    except (TypeError, ValueError):
        rating_val = 4.9
    rating_str = f"{rating_val:.1f}".replace(".", ",")

    raw_old = product.get("preco_original", product.get("preco_antigo", "0,00"))
    raw_promo = product.get("preco_promocional", "0,00")
    old_price = _format_brl(raw_old)
    promo_price = _format_brl(raw_promo)

    link = product.get("link_produto") or product.get("link") or "https://exemplo.com"

    # Só exibe o "De:" riscado quando há desconto real (preço original maior que o promocional)
    linha_de = f"❌ De: <s>R$ {old_price}</s>\n" if old_price != promo_price else ""

    return (
        f"{emojis} <b>{title}</b>\n"
        f"⭐ Avaliação: <b>{rating_str}/5</b>\n\n"
        f"{linha_de}"
        f"✅ <b>Por: R$ {promo_price}</b>\n\n"
        f"🛒 <a href=\"{link}\">Compre aqui com desconto</a>"
    )


def _download_image_bytes(image_url: str) -> Optional[bytes]:
    """Baixa os bytes da imagem em JPEG/PNG para envio direto ao Telegram (rejeita GIFs de placeholder)."""
    import urllib.parse

    try:
        safe_url = urllib.parse.quote(image_url.strip(), safe=":/?&=%-+._~")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "image/jpeg,image/png,*/*;q=0.8",
        }
        resp = requests.get(safe_url, headers=headers, timeout=15)
        if (
            resp.status_code == 200
            and resp.content
            and len(resp.content) > 1000
            and not resp.content.startswith(b"GIF")
        ):
            return resp.content
    except Exception as exc:
        logger.warning("Não foi possível baixar imagem %s: %s", image_url, exc)
    return None


async def publish_product_to_telegram(product_data: dict[str, Any]) -> bool:
    """
    Função assíncrona que recebe os dados do produto (dict), lê as credenciais
    do .env, seleciona a thread correta pela categoria e dispara no Telegram
    com foto (send_photo) quando houver imagem_url, ou texto (send_message).
    """
    load_dotenv(override=True)

    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        logger.error("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados no arquivo .env.")
        return False

    category = str(product_data.get("categoria", "")).lower()
    thread_id = _resolve_thread_id(category)
    message_html = _build_html_message(product_data)
    image_url = product_data.get("imagem_url") or product_data.get("image_url")

    bot = Bot(token=token)
    try:
        async with bot:
            if image_url:
                image_bytes = _download_image_bytes(str(image_url))
                photo_payload: Any = image_bytes if image_bytes else str(image_url)
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_payload,
                    caption=message_html,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread_id,
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=message_html,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread_id,
                    disable_web_page_preview=False,
                )
        logger.info(
            "Oferta '%s' enviada com sucesso para o chat %s (thread_id=%s, com_foto=%s).",
            product_data.get("titulo"),
            chat_id,
            thread_id,
            bool(image_url),
        )
        return True
    except TelegramError as exc:
        logger.error(
            "Falha ao publicar oferta '%s' no Telegram: %s",
            product_data.get("titulo"),
            exc,
        )
        return False


class TelegramPublisher(BasePublisher):
    """Publicador para canais e supergrupos do Telegram com suporte a Tópicos (Threads)."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        topic_map: Optional[dict[Category, int]] = None,
    ) -> None:
        load_dotenv()
        self.bot_token = bot_token or os.getenv("TELEGRAM_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.bot: Optional[Bot] = Bot(token=self.bot_token) if self.bot_token else None
        self.topic_map = topic_map or {}

    def get_topic_id(self, category: Category) -> Optional[int]:
        """Retorna o message_thread_id correspondente à categoria da oferta."""
        if category in self.topic_map and self.topic_map[category] > 0:
            return self.topic_map[category]
        return _resolve_thread_id(category.value)

    async def publish(self, promotion: FormattedPromotion) -> bool:
        """Envia uma FormattedPromotion para o chat/tópico específico no Telegram."""
        if not self.bot or not self.chat_id:
            logger.error("Bot do Telegram não inicializado por falta de credenciais.")
            return False

        thread_id = self.get_topic_id(promotion.category)
        try:
            async with self.bot:
                if promotion.image_url:
                    image_bytes = _download_image_bytes(str(promotion.image_url))
                    photo_payload: Any = image_bytes if image_bytes else str(promotion.image_url)
                    await self.bot.send_photo(
                        chat_id=self.chat_id,
                        photo=photo_payload,
                        caption=promotion.telegram_text,
                        parse_mode=ParseMode.HTML,
                        message_thread_id=thread_id,
                    )
                else:
                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=promotion.telegram_text,
                        parse_mode=ParseMode.HTML,
                        message_thread_id=thread_id,
                    )
            return True
        except TelegramError as exc:
            logger.error("Erro ao publicar no Telegram: %s", exc)
            return False
