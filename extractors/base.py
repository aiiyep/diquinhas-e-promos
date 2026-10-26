from abc import ABC, abstractmethod
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, HttpUrl


class Category(str, Enum):
    MODA = "moda"
    BELEZA = "beleza"
    TECNOLOGIA = "tecnologia"


class Marketplace(str, Enum):
    AMAZON = "amazon"
    SHOPEE = "shopee"
    MERCADO_LIVRE = "mercado_livre"


class ProductOffer(BaseModel):
    """Modelo padronizado de oferta normalizada entre diferentes lojas."""

    id: str = Field(..., description="Identificador único do produto na plataforma")
    title: str = Field(..., description="Título do produto")
    original_price: Optional[Decimal] = Field(None, description="Preço original (De)")
    promo_price: Decimal = Field(..., description="Preço promocional atual (Por)")
    discount_percentage: Optional[int] = Field(None, description="Percentual de desconto")
    product_url: HttpUrl = Field(..., description="URL original do produto")
    affiliate_url: Optional[HttpUrl] = Field(None, description="URL com tag de afiliado")
    image_url: Optional[HttpUrl] = Field(None, description="URL da imagem principal")
    category: Category = Field(..., description="Categoria principal da oferta")
    marketplace: Marketplace = Field(..., description="Loja de origem da oferta")
    coupon_code: Optional[str] = Field(None, description="Cupom aplicável, se houver")


class BaseExtractor(ABC):
    """Interface base para extratores de ofertas de marketplaces."""

    def __init__(self, marketplace: Marketplace) -> None:
        self.marketplace = marketplace

    @abstractmethod
    def fetch_offers(self, category: Category, limit: int = 10) -> list[ProductOffer]:
        """Busca ofertas brutas na API da plataforma e retorna uma lista normalizada."""
        pass

    @abstractmethod
    def _normalize_payload(self, raw_item: dict[str, Any], category: Category) -> ProductOffer:
        """Converte o payload específico da loja para o contrato único ProductOffer."""
        pass
