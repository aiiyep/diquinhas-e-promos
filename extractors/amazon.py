from typing import Any
import requests
from extractors.base import BaseExtractor, Category, Marketplace, ProductOffer


class AmazonExtractor(BaseExtractor):
    """Extrator isolado para a Amazon Product Advertising API (PA-API 5.0)."""

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        partner_tag: str,
        region: str = "us-east-1",
    ) -> None:
        super().__init__(marketplace=Marketplace.AMAZON)
        self.access_key = access_key
        self.secret_key = secret_key
        self.partner_tag = partner_tag
        self.region = region
        self.session = requests.Session()

    def fetch_offers(self, category: Category, limit: int = 10) -> list[ProductOffer]:
        """Busca ofertas na Amazon filtradas por categoria."""
        # TODO: Implementar assinatura AWS Signature V4 e chamada ao endpoint SearchItems
        pass

    def _normalize_payload(self, raw_item: dict[str, Any], category: Category) -> ProductOffer:
        """Normaliza o retorno JSON da PA-API 5.0 para ProductOffer."""
        # TODO: Mapear campos ItemsResult.Items para ProductOffer
        pass
