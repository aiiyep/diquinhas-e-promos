from typing import Any
import requests
from extractors.base import BaseExtractor, Category, Marketplace, ProductOffer


class MercadoLivreExtractor(BaseExtractor):
    """Extrator isolado para a API oficial do Mercado Livre."""

    def __init__(self, app_id: str, client_secret: str, access_token: str) -> None:
        super().__init__(marketplace=Marketplace.MERCADO_LIVRE)
        self.app_id = app_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.session = requests.Session()

    def fetch_offers(self, category: Category, limit: int = 10) -> list[ProductOffer]:
        """Busca ofertas e promoções ativas no Mercado Livre por categoria."""
        # TODO: Implementar busca em /sites/MLB/search com filtros de desconto e categoria
        pass

    def _normalize_payload(self, raw_item: dict[str, Any], category: Category) -> ProductOffer:
        """Normaliza o item retornado pelo Mercado Livre para ProductOffer."""
        # TODO: Mapear campos do resultado MLB para ProductOffer
        pass
