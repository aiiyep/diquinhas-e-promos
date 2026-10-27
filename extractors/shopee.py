import csv
import hashlib
from html import unescape
import logging
from pathlib import Path
import re
import time
from typing import Any, Optional

import requests

from config import env
from models import ProdutoOferta

logger = logging.getLogger("diquinhas_e_promos.shopee")

_OG_IMAGE_PATTERNS = (
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE),
)


class ShopeeExtractor:
    """
    Extrator de ofertas da Shopee com suporte a:
    1. Leitura de arquivos .csv exportados da Shopee Afiliados (para o tópico 'beleza_premium').
    2. Foto oficial do anúncio via og:image do link do produto.
    """

    GRAPHQL_URL = "https://open-api.affiliate.shopee.com.br/graphql"

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        affiliate_id: Optional[str] = None,
    ) -> None:
        self.app_id = (app_id or env("SHOPEE_APP_KEY")).strip()
        self.app_secret = (app_secret or env("SHOPEE_APP_SECRET")).strip()
        self.affiliate_id = (affiliate_id or env("SHOPEE_AFFILIATE_ID")).strip()

    @staticmethod
    def _parse_price(raw_val: Any) -> Optional[float]:
        """Converte strings de preço de CSV (ex: 'R$ 129,90', '129.90', '89,90') para float."""
        if raw_val is None:
            return None
        text = str(raw_val).strip()
        if not text:
            return None
        text = re.sub(r"[^\d,\.]", "", text)
        if not text:
            return None
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            val = float(text)
            return val if val > 0 else None
        except ValueError:
            return None

    @staticmethod
    def _parse_sales(raw_val: Any) -> int:
        """Converte strings de vendas da Shopee (ex: '2mil+', '5mil+', '466') para inteiro."""
        if raw_val is None:
            return 0
        text = str(raw_val).strip().lower().replace(".", "").replace("+", "")
        if not text:
            return 0
        if "mil" in text:
            num_part = re.sub(r"[^\d,]", "", text.replace("mil", "")).replace(",", ".")
            try:
                return int(float(num_part) * 1000) if num_part else 1000
            except ValueError:
                return 1000
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else 0

    def extrair_de_csv(
        self,
        csv_path: str | Path,
        categoria: str = "beleza_premium",
        emojis: str = "💎✨💄",
        limit: Optional[int] = None,
        resolver_imagens: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Lê um arquivo .csv de produtos da Shopee, mapeia dinamicamente as colunas
        (título, preço original/desconto, link de afiliado/produto, imagem),
        ordena pelos mais vendidos, valida com ProdutoOferta e retorna uma lista de
        dicionários prontos para o tópico 'beleza_premium'.
        Linhas sem preço são descartadas. Com `resolver_imagens=False` a foto só é
        buscada depois, para o produto escolhido (`buscar_imagem_oficial`).
        """
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Arquivo CSV da Shopee não encontrado: {path}")

        candidatos: list[dict[str, Any]] = []
        seen_links: set[str] = set()

        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel

            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                if not row:
                    continue
                normalized_row = {
                    (k or "").strip().lower(): (v or "").strip()
                    for k, v in row.items()
                    if k is not None
                }

                titulo = (
                    normalized_row.get("item name")
                    or normalized_row.get("product name")
                    or normalized_row.get("nome do produto")
                    or normalized_row.get("nome do item")
                    or normalized_row.get("titulo")
                    or normalized_row.get("título")
                    or normalized_row.get("name")
                    or normalized_row.get("title")
                )
                link = (
                    normalized_row.get("offer link")
                    or normalized_row.get("short link")
                    or normalized_row.get("affiliate link")
                    or normalized_row.get("link de afiliado")
                    or normalized_row.get("link da oferta")
                    or normalized_row.get("product link")
                    or normalized_row.get("link do produto")
                    or normalized_row.get("link")
                    or normalized_row.get("url")
                )
                imagem = (
                    normalized_row.get("image url")
                    or normalized_row.get("image")
                    or normalized_row.get("imagem")
                    or normalized_row.get("imagem_url")
                    or normalized_row.get("cover image")
                )
                raw_promo = (
                    normalized_row.get("price")
                    or normalized_row.get("sale price")
                    or normalized_row.get("promo price")
                    or normalized_row.get("preço")
                    or normalized_row.get("preco")
                    or normalized_row.get("preço promocional")
                    or normalized_row.get("preco_promocional")
                )
                raw_orig = (
                    normalized_row.get("original price")
                    or normalized_row.get("preço original")
                    or normalized_row.get("preco_original")
                    or normalized_row.get("old price")
                )
                raw_discount = (
                    normalized_row.get("discount")
                    or normalized_row.get("price discount rate")
                    or normalized_row.get("desconto")
                )
                raw_sales = normalized_row.get("sales") or normalized_row.get("vendas")
                raw_rating = (
                    normalized_row.get("rating")
                    or normalized_row.get("rating star")
                    or normalized_row.get("avaliacao")
                    or normalized_row.get("avaliação")
                )
                raw_item_id = (
                    normalized_row.get("item id")
                    or normalized_row.get("item_id")
                    or normalized_row.get("id")
                )

                promo_price = self._parse_price(raw_promo)
                if not titulo or not link or not promo_price or link in seen_links:
                    continue
                seen_links.add(link)

                # Preço "De" só vem de dado real do CSV (coluna própria ou % de desconto informado)
                orig_price = self._parse_price(raw_orig)
                if not orig_price or orig_price <= promo_price:
                    disc_pct = self._parse_price(raw_discount)
                    orig_price = (
                        round(promo_price / (1.0 - (disc_pct / 100.0)), 2)
                        if disc_pct and 0 < disc_pct < 95
                        else None
                    )

                parsed_rating = self._parse_price(raw_rating)
                if parsed_rating and not (0 < parsed_rating <= 5.0):
                    parsed_rating = None

                candidatos.append(
                    {
                        "id": f"SHOPEE{raw_item_id}" if raw_item_id else "",
                        "titulo": titulo,
                        "preco_original": orig_price,
                        "preco_promocional": promo_price,
                        "link_produto": link,
                        "imagem_url": imagem,
                        "sales": self._parse_sales(raw_sales),
                        "avaliacao": parsed_rating,
                    }
                )

        # Prioriza produtos com maior volume de vendas no CSV
        candidatos.sort(key=lambda x: x["sales"], reverse=True)
        if limit:
            candidatos = candidatos[:limit]

        ofertas: list[dict[str, Any]] = []
        for cand in candidatos:
            cand.pop("sales")
            oferta_obj = ProdutoOferta(**cand, categoria=categoria, emojis=emojis, loja="shopee")
            if not oferta_obj.imagem_url and resolver_imagens:
                oferta_obj.imagem_url = self.buscar_imagem_oficial(oferta_obj.link_produto)
            ofertas.append(oferta_obj.model_dump())

        logger.info(
            "Extraídas %d ofertas do CSV '%s' para a categoria '%s'.",
            len(ofertas),
            path.name,
            categoria,
        )
        return ofertas

    def extrair_de_pastas_csv(
        self,
        base_dir: str | Path = "data/shopee",
        por_marca: int = 1,
        categoria: str = "beleza_premium",
        emojis: str = "💎✨💄",
        resolver_imagens: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Percorre todas as subpastas de marcas dentro de `base_dir` (ex: wella, amend,
        dove, haskell, loreal, elseve), lê os arquivos .csv e seleciona até `por_marca`
        produtos de cada marca para publicar no tópico 'beleza_premium'.
        """
        root = Path(base_dir)
        if not root.exists():
            raise FileNotFoundError(f"Diretório de CSVs da Shopee não encontrado: {root}")

        todas_ofertas: list[dict[str, Any]] = []
        seen_titles: set[str] = set()

        # Agrupa arquivos CSV por pasta de marca (ou na raiz se houver CSVs soltos)
        subdirs = sorted([d for d in root.iterdir() if d.is_dir()])
        target_dirs = subdirs if subdirs else [root]

        for folder in target_dirs:
            csv_files = sorted(folder.glob("*.csv"))
            if not csv_files:
                continue

            colhidos_marca = 0
            for csv_file in csv_files:
                if colhidos_marca >= por_marca:
                    break
                itens = self.extrair_de_csv(
                    csv_file,
                    categoria=categoria,
                    emojis=emojis,
                    limit=por_marca * 2,
                    resolver_imagens=resolver_imagens,
                )
                for item in itens:
                    chave = item["titulo"].strip().lower()
                    if chave in seen_titles:
                        continue
                    seen_titles.add(chave)
                    item["marca"] = folder.name
                    todas_ofertas.append(item)
                    colhidos_marca += 1
                    if colhidos_marca >= por_marca:
                        break

        logger.info(
            "Total de %d ofertas da Shopee extraídas de %d pastas para '%s'.",
            len(todas_ofertas),
            len(target_dirs),
            categoria,
        )
        return todas_ofertas

    def buscar_imagem_oficial(self, product_url: str) -> Optional[str]:
        """Extrai a foto oficial do próprio anúncio da Shopee via OpenGraph (og:image)."""
        if not product_url.startswith("http"):
            return None
        try:
            resp = requests.get(
                product_url,
                headers={"User-Agent": "WhatsApp/2.23.20.0"},
                timeout=10,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                for pattern in _OG_IMAGE_PATTERNS:
                    match = pattern.search(resp.text)
                    if match:
                        candidate_img = unescape(match.group(1)).strip()
                        if candidate_img.startswith("http"):
                            return candidate_img
        except Exception as exc:
            logger.debug("Aviso ao ler og:image do link Shopee %s: %s", product_url, exc)
        return None

    def _has_valid_graphql_credentials(self) -> bool:
        return bool(
            self.app_id
            and self.app_secret
            and self.app_id != "seu_shopee_app_id_aqui"
            and self.app_secret != "seu_shopee_app_secret_aqui"
        )

    def _build_graphql_headers(self, payload_str: str) -> dict[str, str]:
        timestamp = int(time.time())
        factor = f"{self.app_id}{timestamp}{payload_str}{self.app_secret}"
        signature = hashlib.sha256(factor.encode("utf-8")).hexdigest()
        return {
            "Content-Type": "application/json",
            "Authorization": (
                f"SHA256 Credential={self.app_id}, "
                f"Timestamp={timestamp}, "
                f"Signature={signature}"
            ),
        }
