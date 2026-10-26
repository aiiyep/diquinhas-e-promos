from extractors.amazon import AmazonExtractor
from extractors.base import BaseExtractor, Category, Marketplace, ProductOffer
from extractors.mercadolivre import MercadoLivreExtractor, ProdutoOferta
from extractors.shopee import ShopeeExtractor

__all__ = [
    "BaseExtractor",
    "Category",
    "Marketplace",
    "ProductOffer",
    "ProdutoOferta",
    "AmazonExtractor",
    "ShopeeExtractor",
    "MercadoLivreExtractor",
]
