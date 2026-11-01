"""
Avisos no privado do Telegram para quem administra o bot (ex.: post do Instagram esperando aprovação).

Usa o mesmo bot que posta no grupo (`TELEGRAM_TOKEN`), mandando para `TELEGRAM_ADMIN_CHAT_ID`, o id
da conversa privada com o bot (a pessoa precisa ter mandado /start para ele antes). O botão "Abrir
painel" leva para `PAINEL_URL` (o endereço https do Tailscale), se estiver configurado.
"""

import logging
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from config import env
from publishers.telegram_publisher import _get_bot

logger = logging.getLogger("diquinhas_e_promos.avisos")

LIMITE_LEGENDA_FOTO = 1024  # limite do Telegram para legenda de foto


def avisos_configurados() -> bool:
    return bool(env("TELEGRAM_TOKEN") and env("TELEGRAM_ADMIN_CHAT_ID"))


def _botao_painel(ancora: str = "") -> Optional[InlineKeyboardMarkup]:
    url = env("PAINEL_URL").rstrip("/")
    if not url.startswith("https://"):
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("Abrir painel", url=f"{url}/{ancora}")]])


async def avisar_admin(texto_html: str, foto: Optional[Path] = None, ancora: str = "") -> bool:
    """Manda o aviso (com foto, se houver). Devolve False sem configuração ou se o Telegram recusar."""
    if not avisos_configurados():
        logger.warning("Aviso não enviado: configure TELEGRAM_ADMIN_CHAT_ID no .env. Texto: %s", texto_html[:120])
        return False
    try:
        bot = await _get_bot(env("TELEGRAM_TOKEN"))
        chat_id = env("TELEGRAM_ADMIN_CHAT_ID")
        teclado = _botao_painel(ancora)
        if foto is not None and foto.is_file():
            await bot.send_photo(
                chat_id=chat_id, photo=foto.read_bytes(), caption=texto_html[:LIMITE_LEGENDA_FOTO],
                parse_mode=ParseMode.HTML, reply_markup=teclado,
            )
        else:
            await bot.send_message(
                chat_id=chat_id, text=texto_html, parse_mode=ParseMode.HTML, reply_markup=teclado,
                disable_web_page_preview=True,
            )
        return True
    except Exception as exc:
        logger.error("Falha ao mandar aviso no Telegram: %s", exc)
        return False
