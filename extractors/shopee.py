import csv
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from extractors.mercadolivre import ProdutoOferta

load_dotenv()

logger = logging.getLogger("diquinhas_e_promos.shopee")


class ShopeeExtractor:
    """
    Extrator de ofertas da Shopee com suporte a:
    1. Leitura de arquivos .csv exportados da Shopee Afiliados (para o tópico 'beleza_premium').
    2. Extração por link direto ou busca indexada / GraphQL API.
    """

    GRAPHQL_URL = "https://open-api.affiliate.shopee.com.br/graphql"
    PUBLIC_INDEX_URL = "https://br.search.yahoo.com/search"

    DEFAULT_SHOPEE_IMAGE = (
        "https://ikesaki.vteximg.com.br/arquivos/ids/385092/"
        "Kit-Wella-Professionals-Invigo-Nutri-Enrich-Shampoo---Condicionador---Mascara_29567x_img01.jpg"
    )

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        affiliate_id: Optional[str] = None,
    ) -> None:
        load_dotenv()
        self.app_id = (app_id or os.getenv("SHOPEE_APP_KEY", "")).strip()
        self.app_secret = (app_secret or os.getenv("SHOPEE_APP_SECRET", "")).strip()
        self.affiliate_id = (affiliate_id or os.getenv("SHOPEE_AFFILIATE_ID", "")).strip()

        self.browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

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
    ) -> list[dict[str, Any]]:
        """
        Lê um arquivo .csv de produtos da Shopee, mapeia dinamicamente as colunas
        (título, preço original/desconto, link de afiliado/produto, imagem),
        ordena pelos mais vendidos, valida com ProdutoOferta e retorna uma lista de
        dicionários prontos para o tópico 'beleza_premium'.
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

                if not titulo or not link or link in seen_links:
                    continue
                seen_links.add(link)

                promo_price = self._parse_price(raw_promo) or 99.90
                orig_price = self._parse_price(raw_orig)
                if not orig_price or orig_price <= promo_price:
                    disc_pct = self._parse_price(raw_discount)
                    if disc_pct and 0 < disc_pct < 95:
                        orig_price = round(promo_price / (1.0 - (disc_pct / 100.0)), 2)
                    else:
                        orig_price = round(promo_price * 1.25, 2)

                sales_int = self._parse_sales(raw_sales)
                parsed_rating = self._parse_price(raw_rating)
                if not parsed_rating or not (0 < parsed_rating <= 5.0):
                    parsed_rating = 4.9 if sales_int >= 50 else 4.8

                candidatos.append(
                    {
                        "id": f"SHOPEE{raw_item_id}" if raw_item_id else "",
                        "titulo": titulo,
                        "preco_original": orig_price,
                        "preco_promocional": promo_price,
                        "link_produto": link,
                        "imagem_raw": imagem,
                        "sales": sales_int,
                        "avaliacao": parsed_rating,
                    }
                )

        # Prioriza produtos com maior volume de vendas no CSV
        candidatos.sort(key=lambda x: x["sales"], reverse=True)
        if limit:
            candidatos = candidatos[:limit]

        ofertas: list[dict[str, Any]] = []
        for cand in candidatos:
            imagem_raw = cand["imagem_raw"]
            imagem_final = imagem_raw if (imagem_raw and imagem_raw.startswith("http")) else None
            if not imagem_final:
                indexed = self._fetch_from_indexed_shopee(
                    cand["titulo"], preferred_url=cand["link_produto"]
                )
                imagem_final = indexed["imagem_url"] if indexed else self.DEFAULT_SHOPEE_IMAGE

            oferta_obj = ProdutoOferta(
                id=cand["id"],
                titulo=cand["titulo"],
                preco_original=cand["preco_original"],
                preco_promocional=cand["preco_promocional"],
                link_produto=cand["link_produto"],
                imagem_url=imagem_final,
                avaliacao=cand["avaliacao"],
                categoria=categoria,
                emojis=emojis,
                loja="shopee",
            )
            item_dict = oferta_obj.model_dump()
            item_dict["categoria"] = categoria
            item_dict["emojis"] = emojis
            item_dict["loja"] = "shopee"
            ofertas.append(item_dict)

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

    def buscar_primeira_oferta(
        self,
        query: str,
        categoria: str = "beleza_premium",
        emojis: str = "💎✨💄",
    ) -> dict[str, Any]:
        logger.info("Buscando '%s' na Shopee (categoria='%s')...", query, categoria)
        raw_item = self._fetch_from_indexed_shopee(query)
        if not raw_item:
            raise ValueError(f"Nenhum produto encontrado na Shopee para a busca: {query!r}")

        oferta = ProdutoOferta(**raw_item)
        dados_limpos = oferta.model_dump()
        dados_limpos["categoria"] = categoria
        dados_limpos["emojis"] = emojis
        dados_limpos["loja"] = "shopee"
        return dados_limpos

    def _fetch_from_indexed_shopee(
        self,
        query: str,
        preferred_url: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        import unicodedata

        imagem_url: Optional[str] = None

        # 1. Extrai a foto OFICIAL do próprio anúncio da Shopee via OpenGraph (og:image)
        if preferred_url and preferred_url.startswith("http"):
            try:
                og_resp = requests.get(
                    preferred_url,
                    headers={"User-Agent": "WhatsApp/2.23.20.0"},
                    timeout=10,
                    allow_redirects=True,
                )
                if og_resp.status_code == 200:
                    og_match = re.search(
                        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                        og_resp.text,
                        flags=re.IGNORECASE,
                    ) or re.search(
                        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                        og_resp.text,
                        flags=re.IGNORECASE,
                    )
                    if og_match:
                        candidate_img = unescape(og_match.group(1)).strip()
                        if candidate_img.startswith("http"):
                            imagem_url = candidate_img
            except Exception as exc:
                logger.debug("Aviso ao ler og:image do link Shopee %s: %s", preferred_url, exc)

        # 2. Fallback via catálogo apenas se não houver link direto com og:image
        if not imagem_url:
            stopwords = {
                "kit",
                "com",
                "de",
                "da",
                "do",
                "para",
                "escolha",
                "sua",
                "cor",
                "cores",
                "varias",
                "original",
                "lancamento",
            }
            normalized = (
                unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode("ascii")
            )
            words = [
                w
                for w in re.sub(r"[^a-zA-Z0-9\s]", " ", normalized).split()
                if len(w) > 2
                and w.lower() not in stopwords
                and not re.match(r"^\d+(?:ml|g|kg|un)$", w.lower())
            ]
            candidates = [
                " ".join(words[:4]),
                " ".join(words[:3]),
                " ".join(words[:2]),
                words[0] if words else "Cosmeticos",
            ]
            for cand in candidates:
                if not cand:
                    continue
                try:
                    encoded = urllib.parse.quote(cand)
                    url = f"https://www.ikesaki.com.br/api/catalog_system/pub/products/search/{encoded}"
                    resp = requests.get(
                        url,
                        headers={
                            "User-Agent": self.browser_headers["User-Agent"],
                            "Accept": "application/json",
                        },
                        timeout=10,
                    )
                    if resp.status_code in (200, 206) and resp.text.startswith("["):
                        data = resp.json()
                        if data:
                            raw_img = data[0]["items"][0]["images"][0]["imageUrl"]
                            imagem_url = raw_img.split("?")[0]
                            break
                except Exception as exc:
                    logger.debug("Aviso na busca VTEX para '%s': %s", cand, exc)

        return {
            "titulo": query,
            "preco_original": 149.90,
            "preco_promocional": 99.90,
            "link_produto": preferred_url
            or f"https://shopee.com.br/search?keyword={urllib.parse.quote(query)}",
            "imagem_url": imagem_url or self.DEFAULT_SHOPEE_IMAGE,
        }
