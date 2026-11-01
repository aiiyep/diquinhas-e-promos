"""
Texto dos posts do canal do WhatsApp.

O canal é alimentado à mão (semiautomático): o painel mostra a fila de postagens já feitas no
Telegram e este módulo monta o texto pronto para copiar e colar, com a formatação do WhatsApp
(*negrito*, ~riscado~). Não há envio automático: a API oficial não posta em canais e as não
oficiais arriscam banir o número.
"""

import re
from typing import Any, Optional

from publishers.telegram_publisher import _format_brl, _to_float

# Caracteres que o WhatsApp interpreta como formatação e quebrariam o título
_MARCADORES = re.compile(r"[*_~`]+")

# Produto de catálogo do ML: o nome no meio do endereço é enfeite (o ML abre só com /p/MLB…)
_ML_CATALOGO = re.compile(r"^https?://(?:www\.)?mercadolivre\.com\.br/(?:[^?#]*/)?p/(MLB\d+)(\?[^#]*)?", re.IGNORECASE)


def encurtar_link(link: str) -> str:
    """
    Tira o nome do produto do link de catálogo do ML (186 → ~90 caracteres). Os parâmetros do fim
    (matt_word/matt_tool, que dão a comissão) ficam intactos. Outros links ficam como estão.
    """
    m = _ML_CATALOGO.match(link.strip())
    if not m:
        return link.strip()
    return f"https://www.mercadolivre.com.br/p/{m.group(1).upper()}{m.group(2) or ''}"


def _limpar(texto: str) -> str:
    return " ".join(_MARCADORES.sub(" ", texto).split())


def montar_texto_whatsapp(post: dict[str, Any]) -> str:
    """
    Legenda do post para o canal, no mesmo modelo da do Telegram. A linha ⭐ e o "De" riscado
    só aparecem quando há nota e desconto reais.
    """
    emojis = _limpar(str(post.get("emojis") or "🔥"))
    titulo = _limpar(str(post.get("titulo") or "Oferta"))
    link = encurtar_link(str(post.get("link") or post.get("link_produto") or ""))

    promo = _to_float(post.get("preco") if post.get("preco") is not None else post.get("preco_promocional"))
    orig = _to_float(post.get("preco_original"))
    nota = _to_float(post.get("avaliacao"))

    linhas = [f"{emojis} *{titulo}*"]
    if nota:
        linhas.append(f"⭐ Avaliação: {nota:.1f}/5".replace(".", ","))
    linhas.append("")
    desconto = percentual_desconto(orig, promo)
    if desconto:
        linhas.append(f"❌ De: ~R$ {_format_brl(orig)}~")
    if promo:
        linhas.append(f"✅ *Por: R$ {_format_brl(promo)}*" + (f" ({desconto}% OFF)" if desconto else ""))
    linhas.append("")
    linhas.append(f"🛒 Compre aqui: {link}")
    return "\n".join(linhas)


def percentual_desconto(orig: Optional[float], promo: Optional[float]) -> Optional[int]:
    if orig and promo and orig > promo:
        return round((1 - promo / orig) * 100) or None
    return None
