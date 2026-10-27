import asyncio
from html import escape
import logging
from typing import Any, Optional
import urllib.parse

import requests
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import BROWSER_USER_AGENT, env

logger = logging.getLogger("diquinhas_e_promos.telegram")

# Bot reaproveitado entre os envios (inicializado na primeira publicação)
_bot: Optional[Bot] = None


def _resolve_thread_id(category: str) -> Optional[int]:
    """Mapeia a categoria do produto para o message_thread_id configurado no .env."""
    thread_mapping: dict[str, str] = {
        "rosto_e_corpo": env("THREAD_ID_ROSTO_E_CORPO") or env("THREAD_ID_SKINCARE"),
        "rosto e corpo": env("THREAD_ID_ROSTO_E_CORPO") or env("THREAD_ID_SKINCARE"),
        "cabelo": env("THREAD_ID_CABELO"),
        "unhas": env("THREAD_ID_UNHAS"),
        "moda": env("THREAD_ID_MODA"),
        "skincare": env("THREAD_ID_SKINCARE") or env("THREAD_ID_PROTETOR_SOLAR"),
        "protetor_solar": env("THREAD_ID_PROTETOR_SOLAR"),
        "protetor solar": env("THREAD_ID_PROTETOR_SOLAR"),
        "hidratante": env("THREAD_ID_HIDRATANTE"),
        "limpeza_facial": env("THREAD_ID_LIMPEZA_FACIAL"),
        "limpeza facial": env("THREAD_ID_LIMPEZA_FACIAL"),
        "seruns_e_acidos": env("THREAD_ID_SERUNS_E_ACIDOS"),
        "séruns e ácidos": env("THREAD_ID_SERUNS_E_ACIDOS"),
        "k-beauty": env("THREAD_ID_KBEAUTY"),
        "kbeauty": env("THREAD_ID_KBEAUTY"),
        "beleza_premium": env("THREAD_ID_BELEZA_PREMIUM"),
        "beleza premium": env("THREAD_ID_BELEZA_PREMIUM"),
        "ofertas": env("THREAD_ID_OFERTAS"),
        "tecnologia": env("THREAD_ID_TECNOLOGIA") or env("THREAD_ID_OFERTAS"),
    }
    raw_thread_id = thread_mapping.get(category.strip().lower())
    if raw_thread_id and raw_thread_id.isdigit():
        thread_id = int(raw_thread_id)
        return thread_id if thread_id > 0 else None
    return None


def _format_brl(value: float) -> str:
    """Formata um float para o padrão monetário brasileiro (1.234,56)."""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _build_html_message(product: dict[str, Any]) -> str:
    """
    Monta a legenda em HTML a partir do dicionário do produto.
    A linha ⭐ e o "De:" riscado só aparecem quando há nota e desconto reais.
    """
    emojis = escape(str(product.get("emojis") or "🔥"))
    title = escape(str(product.get("titulo") or "Oferta"))
    link = escape(str(product.get("link_produto") or product.get("link") or ""), quote=True)

    promo = _to_float(product.get("preco_promocional"))
    orig = _to_float(product.get("preco_original"))
    rating = _to_float(product.get("avaliacao"))

    linhas = [f"{emojis} <b>{title}</b>"]
    if rating:
        linhas.append(f"⭐ Avaliação: <b>{rating:.1f}/5</b>".replace(".", ","))
    linhas.append("")
    if orig and promo and orig > promo:
        linhas.append(f"❌ De: <s>R$ {_format_brl(orig)}</s>")
    if promo:
        linhas.append(f"✅ <b>Por: R$ {_format_brl(promo)}</b>")
    linhas.append("")
    linhas.append(f"🛒 <a href=\"{link}\">Compre aqui</a>")
    return "\n".join(linhas)


def _download_image_bytes(image_url: str) -> Optional[bytes]:
    """Baixa os bytes da imagem em JPEG/PNG para envio direto ao Telegram (rejeita GIFs de placeholder)."""
    try:
        safe_url = urllib.parse.quote(image_url.strip(), safe=":/?&=%-+._~")
        headers = {"User-Agent": BROWSER_USER_AGENT, "Accept": "image/jpeg,image/png,*/*;q=0.8"}
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


async def _get_bot(token: str) -> Bot:
    global _bot
    if _bot is None or _bot.token != token:
        _bot = Bot(token=token)
        await _bot.initialize()
    return _bot


async def publish_product_to_telegram(product_data: dict[str, Any]) -> bool:
    """
    Recebe os dados do produto (dict), seleciona a thread correta pela categoria e
    dispara no Telegram com foto (send_photo) quando houver imagem_url, ou texto (send_message).
    """
    token = env("TELEGRAM_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados no arquivo .env.")
        return False

    category = str(product_data.get("categoria", "")).lower()
    thread_id = _resolve_thread_id(category)
    message_html = _build_html_message(product_data)
    image_url = product_data.get("imagem_url") or product_data.get("image_url")

    try:
        bot = await _get_bot(token)
        if image_url:
            image_bytes = await asyncio.to_thread(_download_image_bytes, str(image_url))
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


async def fechar_bot() -> None:
    """Encerra a sessão HTTP do bot reaproveitado (chamar ao finalizar o processo)."""
    global _bot
    if _bot is not None:
        await _bot.shutdown()
        _bot = None
