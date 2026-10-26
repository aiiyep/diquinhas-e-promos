import os
import urllib.parse
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, HttpUrl

from extractors.base import Category, ProductOffer

load_dotenv()


class FormattedPromotion(BaseModel):
    """Estrutura contendo a oferta pronta para disparo nos canais."""

    offer_id: str
    category: Category
    telegram_text: str
    twitter_text: str
    tracking_url: HttpUrl
    image_url: Optional[HttpUrl] = None


class PromoFormatter:
    """Responsável por montar o copy da promoção e encapsular os links de rastreio."""

    def __init__(self, affiliate_tags: Optional[dict[str, str]] = None) -> None:
        load_dotenv(override=True)
        self.affiliate_tags = affiliate_tags or {}
        self.ml_matt_tool = os.getenv("ML_MATT_TOOL", "").strip()
        self.ml_matt_word = os.getenv("ML_MATT_WORD", "").strip()
        self.amazon_tag = os.getenv("AMAZON_PARTNER_TAG", "").strip()
        self.shopee_affiliate_id = os.getenv("SHOPEE_AFFILIATE_ID", "").strip()

    def gerar_link_afiliado_url(self, url: str) -> str:
        """
        Recebe uma URL de produto (Mercado Livre, Shopee, Amazon, etc.) e injeta
        os parâmetros de rastreio de afiliado configurados no .env.
        """
        # Preserva links curtos já gerados (s.shopee.com.br, mercadolivre.com/sec/, amzn.to)
        if any(short in url.lower() for short in ("s.shopee.com.br", "shp.ee", "mercadolivre.com/sec/", "amzn.to")):
            return url.strip()

        parsed = urllib.parse.urlsplit(url.split("#")[0])
        domain = parsed.netloc.lower()
        query_params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))

        if "mercadolivre.com" in domain or "mercadolibre.com" in domain:
            if self.ml_matt_word:
                query_params["matt_word"] = self.ml_matt_word
            if self.ml_matt_tool:
                query_params["matt_tool"] = self.ml_matt_tool
        elif "shopee.com.br" in domain:
            if self.shopee_affiliate_id and self.shopee_affiliate_id != "seu_shopee_affiliate_id_aqui":
                query_params["utm_source"] = f"an_{self.shopee_affiliate_id}"
                query_params["utm_medium"] = "affiliates"
        elif "amazon.com" in domain and self.amazon_tag:
            query_params["tag"] = self.amazon_tag
            query_params["linkCode"] = "ll1"

        new_query = urllib.parse.urlencode(query_params)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, ""))

    def aplicar_afiliado(self, product_dict: dict[str, Any]) -> dict[str, Any]:
        """
        Recebe o dicionário do produto (vindo do extrator), converte o link_produto
        em link de afiliado rastreável e calcula o percentual de desconto.
        """
        updated = dict(product_dict)
        raw_link = updated.get("link_produto") or updated.get("link")
        if raw_link:
            affiliate_link = self.gerar_link_afiliado_url(str(raw_link))
            updated["link_produto"] = affiliate_link
            updated["link"] = affiliate_link

        orig = updated.get("preco_original")
        promo = updated.get("preco_promocional")
        if isinstance(orig, (int, float)) and isinstance(promo, (int, float)) and orig > promo:
            updated["desconto_pct"] = round(((orig - promo) / orig) * 100)

        return updated

    def build_tracking_link(self, offer: ProductOffer) -> str:
        """Encapsula ou valida o link final de afiliado a partir de um ProductOffer."""
        return self.gerar_link_afiliado_url(str(offer.product_url))

    def format_price_comparison(self, offer: ProductOffer) -> str:
        """Gera o bloco visual de comparativo de preços (De / Por e % de desconto)."""
        promo_str = f"{offer.promo_price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if offer.original_price and offer.original_price > offer.promo_price:
            orig_str = f"{offer.original_price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            return f"❌ De: <s>R$ {orig_str}</s>\n✅ <b>Por: R$ {promo_str}</b>"
        return f"✅ <b>Por: R$ {promo_str}</b>"

    def format_offer(self, offer: ProductOffer) -> FormattedPromotion:
        """Transforma um ProductOffer em textos otimizados para Telegram e Twitter/X."""
        tracking_url = self.build_tracking_link(offer)
        price_block = self.format_price_comparison(offer)
        rating_val = getattr(offer, "rating", 4.9)
        rating_str = f"{float(rating_val):.1f}".replace(".", ",")
        telegram_text = (
            f"🔥 <b>{offer.title}</b>\n"
            f"⭐ Avaliação: <b>{rating_str}/5</b>\n\n"
            f"{price_block}\n\n"
            f"🛒 <a href=\"{tracking_url}\">Compre aqui com desconto</a>"
        )
        twitter_text = f"🔥 {offer.title}\nPor R$ {offer.promo_price}\n👉 {tracking_url}"
        return FormattedPromotion(
            offer_id=offer.id,
            category=offer.category,
            telegram_text=telegram_text,
            twitter_text=twitter_text,
            tracking_url=tracking_url,  # type: ignore[arg-type]
            image_url=offer.image_url,
        )
