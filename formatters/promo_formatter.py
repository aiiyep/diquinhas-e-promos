from typing import Any, Optional
import urllib.parse

from config import env


class PromoFormatter:
    """Responsável por encapsular os links de rastreio de afiliado e calcular o desconto."""

    def __init__(self, affiliate_tags: Optional[dict[str, str]] = None) -> None:
        self.affiliate_tags = affiliate_tags or {}
        self.ml_matt_tool = env("ML_MATT_TOOL")
        self.ml_matt_word = env("ML_MATT_WORD")
        self.amazon_tag = env("AMAZON_PARTNER_TAG")
        self.shopee_affiliate_id = env("SHOPEE_AFFILIATE_ID")

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
