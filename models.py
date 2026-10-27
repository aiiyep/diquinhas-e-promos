import hashlib
from html import unescape
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ProdutoOferta(BaseModel):
    """
    Modelo Pydantic que padroniza as ofertas de todas as lojas.
    `preco_original` e `avaliacao` só são preenchidos com dados reais da loja;
    quando ausentes ficam `None` e não aparecem na postagem.
    """

    id: str = Field(default="", description="Identificador único do produto (ex: MLB123456)")
    titulo: str = Field(..., min_length=1, description="Título limpo do produto")
    preco_original: Optional[float] = Field(default=None, description="Preço 'De' real, se houver desconto")
    preco_promocional: float = Field(..., gt=0, description="Preço atual")
    link_produto: str = Field(..., min_length=5, description="URL direta do produto")
    imagem_url: Optional[str] = Field(default=None, description="URL da imagem do produto")
    avaliacao: Optional[float] = Field(default=None, ge=0.0, le=5.0, description="Nota real (0 a 5)")
    categoria: str = Field(default="skincare", description="Categoria/tópico de destino")
    emojis: str = Field(default="🧴✨☀️", description="Emojis temáticos da oferta")
    loja: str = Field(default="mercado_livre", description="Identificador da loja")

    @field_validator("avaliacao")
    @classmethod
    def round_avaliacao(cls, value: Optional[float]) -> Optional[float]:
        return round(float(value), 1) if value else None

    @field_validator("titulo")
    @classmethod
    def clean_titulo(cls, value: str) -> str:
        cleaned = re.sub(r"<[^>]+>", "", unescape(value))
        cleaned = re.sub(r"\s*\|\s*Mercado\s*Livre.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*-\s*Mercado\s*Livre.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*-\s*R\$.*$", "", cleaned, flags=re.IGNORECASE)
        return " ".join(cleaned.split()).strip()

    @field_validator("preco_promocional")
    @classmethod
    def round_promo(cls, value: float) -> float:
        return round(float(value), 2)

    @field_validator("preco_original")
    @classmethod
    def round_original(cls, value: Optional[float]) -> Optional[float]:
        return round(float(value), 2) if value else None

    @field_validator("imagem_url")
    @classmethod
    def clean_imagem(cls, value: Optional[str]) -> Optional[str]:
        return value if value and value.startswith("http") else None

    @model_validator(mode="after")
    def normalize(self) -> "ProdutoOferta":
        # Só existe "De" quando o preço original é de fato maior que o atual
        if self.preco_original is not None and self.preco_original <= self.preco_promocional:
            self.preco_original = None

        if not self.id or not self.id.strip():
            mlb_match = re.search(r"(MLB-?\d+)", self.link_produto, flags=re.IGNORECASE)
            if mlb_match:
                self.id = mlb_match.group(1).upper().replace("-", "")
            else:
                digest = hashlib.sha1(self.titulo.lower().encode("utf-8")).hexdigest()[:10].upper()
                self.id = f"MLB{digest}"
        return self

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)
