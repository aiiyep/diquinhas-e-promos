import base64
from html import unescape
import logging
import re
import time
from typing import Any, Optional
import unicodedata

import requests

from config import BROWSER_HEADERS, BROWSER_USER_AGENT, env
from models import ProdutoOferta

logger = logging.getLogger("diquinhas_e_promos.mercadolivre")

# Mapeamento dos containers do Mercado Livre enviados pelo usuário por tópico
ML_TOPIC_CONTAINERS: dict[str, list[str]] = {
    "rosto_e_corpo": [
        "https://lista.mercadolivre.com.br/_Container_makes-virais-face#element_position=2&navigation_id=card-banners-1792442107407_2_b971acb8-f61f-4090-8e41-aa64405f529e&container_id=MLB1645009-1",
        "https://lista.mercadolivre.com.br/_Container_makes-virais-labios#element_position=3&navigation_id=card-banners-1792442107407_3_133bd168-37d5-4406-855d-ac4e8c8c7d05&container_id=MLB1644868-1",
        "https://lista.mercadolivre.com.br/_Container_viralizados-skin-care#element_position=4&navigation_id=card-banners-1792442107407_4_a51fe130-e8bc-4feb-b86b-0cd260941464&container_id=MLB1645014-1",
    ],
    "cabelo": [
        "https://lista.mercadolivre.com.br/_Container_hair-beauty#element_position=2&navigation_id=thumbnails-carousel-1791575610632_2_92bb62ff-95da-43e9-8bcf-a5710d6a65ab&container_id=MLB1203742-1",
    ],
    "unhas": [
        "https://lista.mercadolivre.com.br/_Container_beauty-manicure-e-pedicure#element_position=1&navigation_id=secondary-banner-ed997694-c18d-4b74-971a-0004a68cc162_1_b9a2f5cd-e697-46b9-8c7b-787503eee0a8&container_id=MLB1110524-1",
    ],
    "moda": [
        "https://lista.mercadolivre.com.br/_Container_moda-fashion_FILTRABLE*GENDER_18549361#element_position=1&navigation_id=card-banners-1790362368668_1_f2de64ce-f64f-4a05-ac3c-283aedeee64c&container_id=MLB911475-1",
    ],
}

# Termos de busca equivalentes a cada container vitrine do Mercado Livre
CONTAINER_SEARCH_HINTS: dict[str, str] = {
    "makes-virais-face": "Base Liquida Fran",
    "makes-virais-labios": "Gloss Labial",
    "viralizados-skin-care": "Gel Limpeza Facial",
    "hair-beauty": "Mascara Capilar Wella",
    "beauty-manicure-e-pedicure": "Esmalte Risque",
    "moda-fashion": "Vestido Feminino Midi",
}

TOPIC_EMOJIS: dict[str, str] = {
    "rosto_e_corpo": "🧴✨💄",
    "cabelo": "💇‍♀️✨🌸",
    "unhas": "💅✨💖",
    "moda": "👗👠✨",
    "k-beauty": "🌸✨🇰🇷",
}

__all__ = [
    "CONTAINER_SEARCH_HINTS",
    "ML_TOPIC_CONTAINERS",
    "MercadoLivreExtractor",
    "ProdutoOferta",
    "TOPIC_EMOJIS",
]


def _slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


class MercadoLivreExtractor:
    """Extrator de ofertas reais do Mercado Livre com suporte a lista de 20 resultados e containers."""

    SEARCH_API_URL = "https://api.mercadolibre.com/sites/MLB/search"

    # Cache de token OAuth (client_credentials) compartilhado entre instâncias
    _oauth_token: str = ""
    _oauth_expira_em: float = 0.0

    def __init__(self, access_token: Optional[str] = None) -> None:
        self.access_token = (access_token or env("ML_ACCESS_TOKEN")).strip()
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        if self.access_token and self.access_token != "seu_ml_access_token_aqui":
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    @staticmethod
    def _extract_container_slug(container_url: str) -> str:
        """Extrai o identificador do container a partir da URL lista.mercadolivre.com.br/_Container_..."""
        match = re.search(r"_Container_([a-zA-Z0-9\-]+)", container_url)
        return match.group(1).lower() if match else "ofertas"

    def _obter_token_oficial(self) -> str:
        """
        Obtém (e mantém em cache) um access token da API oficial do Mercado Livre via
        `client_credentials`, usando ML_CLIENT_ID e ML_CLIENT_SECRET do `.env`.
        O token dura 6h e é renovado automaticamente 5 minutos antes de expirar.
        """
        client_id = env("ML_CLIENT_ID")
        client_secret = env("ML_CLIENT_SECRET")
        if not client_id or not client_secret:
            return ""

        agora = time.time()
        cls = MercadoLivreExtractor
        if cls._oauth_token and agora < cls._oauth_expira_em - 300:
            return cls._oauth_token

        try:
            resp = requests.post(
                "https://api.mercadolibre.com/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code == 200:
                payload = resp.json()
                cls._oauth_token = str(payload.get("access_token") or "")
                cls._oauth_expira_em = agora + float(payload.get("expires_in") or 21600)
                return cls._oauth_token
            logger.error("Falha ao obter token do Mercado Livre: HTTP %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("Erro ao obter token do Mercado Livre: %s", exc)
        return ""

    def _fetch_list_from_official_catalog(
        self,
        query: str,
        limit: int = 20,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Busca produtos reais no catálogo oficial do Mercado Livre (`/products/search`) e,
        para cada produto, consulta os anúncios ativos (`/products/{id}/items`) para obter
        o menor preço atual. O link aponta direto para a página do produto (`/p/MLB...`).
        """
        token = self._obter_token_oficial()
        if not token:
            return []

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        items_out: list[dict[str, Any]] = []
        try:
            resp = requests.get(
                "https://api.mercadolibre.com/products/search",
                params={
                    "status": "active",
                    "site_id": "MLB",
                    "q": query,
                    "limit": limit,
                    "offset": max(0, page) * limit,
                },
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("Catálogo oficial ML retornou HTTP %s para '%s'.", resp.status_code, query)
                return []

            for prod in resp.json().get("results") or []:
                prod_id = str(prod.get("id") or "")
                nome = str(prod.get("name") or "").strip()
                pictures = prod.get("pictures") or []
                imagem = str(pictures[0].get("url") or "") if pictures else ""
                if not prod_id or not nome or not imagem:
                    continue

                try:
                    items_resp = requests.get(
                        f"https://api.mercadolibre.com/products/{prod_id}/items",
                        headers=headers,
                        timeout=10,
                    )
                    if items_resp.status_code != 200:
                        continue
                    anuncios = [
                        a for a in (items_resp.json().get("results") or [])
                        if float(a.get("price") or 0) > 0
                    ]
                except Exception:
                    continue
                if not anuncios:
                    continue  # produto sem anúncio ativo (indisponível)

                melhor = min(anuncios, key=lambda a: float(a["price"]))
                items_out.append(
                    {
                        "id": prod_id,
                        "titulo": nome,
                        "preco_original": melhor.get("original_price"),
                        "preco_promocional": float(melhor["price"]),
                        "link_produto": f"https://www.mercadolivre.com.br/{_slugify(nome)}/p/{prod_id}",
                        "imagem_url": imagem,
                    }
                )
        except Exception as exc:
            logger.warning("Erro no catálogo oficial ML para '%s': %s", query, exc)
        return items_out

    def buscar_ofertas(
        self,
        query: str,
        categoria: str = "skincare",
        emojis: str = "🧴✨☀️",
        limit: int = 20,
        page: int = 0,
    ) -> list[ProdutoOferta]:
        """
        Busca uma lista de produtos no Mercado Livre (padrão: até os primeiros 20 resultados),
        suportando paginação (`page=0, 1, 2...`) e validando cada item pelo modelo Pydantic `ProdutoOferta`.
        """
        logger.info(
            "Buscando até %d ofertas para '%s' (página %d) no Mercado Livre...",
            limit,
            query,
            page,
        )

        raw_list = self._fetch_list_from_official_catalog(query, limit=limit, page=page)
        if not raw_list:
            raw_list = self._fetch_list_from_public_api(query, limit=limit, page=page)

        if not raw_list:
            raise ValueError(f"Nenhum produto encontrado no Mercado Livre para a busca: {query!r}")

        ofertas: list[ProdutoOferta] = []
        seen_ids: set[str] = set()
        for raw_item in raw_list:
            raw_item["categoria"] = categoria
            raw_item["emojis"] = emojis
            raw_item["loja"] = "mercado_livre"
            oferta = ProdutoOferta(**raw_item)
            if oferta.id not in seen_ids:
                seen_ids.add(oferta.id)
                ofertas.append(oferta)
            if len(ofertas) >= limit:
                break

        logger.info(
            "Retornadas %d ofertas validadas para '%s' (categoria='%s').",
            len(ofertas),
            query,
            categoria,
        )
        return ofertas

    def buscar_primeira_oferta(
        self,
        query: str,
        categoria: str = "skincare",
        emojis: str = "🧴✨☀️",
    ) -> dict[str, Any]:
        """
        Mantém compatibilidade retornando o primeiro produto da lista de ofertas como dicionário.
        """
        ofertas = self.buscar_ofertas(query=query, categoria=categoria, emojis=emojis, limit=20)
        return ofertas[0].model_dump()

    def extrair_de_container(
        self,
        container_url: str,
        categoria: str,
        emojis: Optional[str] = None,
    ) -> dict[str, Any]:
        """Extrai uma oferta a partir de um link de container (_Container_...) do Mercado Livre."""
        slug = self._extract_container_slug(container_url)
        search_hint = CONTAINER_SEARCH_HINTS.get(slug, slug.replace("-", " "))
        chosen_emojis = emojis or TOPIC_EMOJIS.get(categoria, "🔥✨")

        logger.info("Extraindo container ML '%s' (categoria='%s')...", slug, categoria)

        direct_items = self._try_direct_container_scrape(container_url, limit=1)
        if direct_items:
            oferta = ProdutoOferta(
                **direct_items[0],
                categoria=categoria,
                emojis=chosen_emojis,
                loja="mercado_livre",
            )
            return oferta.model_dump()

        return self.buscar_primeira_oferta(
            query=search_hint,
            categoria=categoria,
            emojis=chosen_emojis,
        )

    def extrair_lista_de_container(
        self,
        container_url: str,
        categoria: str,
        emojis: Optional[str] = None,
        limit: int = 20,
    ) -> list[ProdutoOferta]:
        """Extrai até `limit` (20) produtos de um container do Mercado Livre."""
        slug = self._extract_container_slug(container_url)
        search_hint = CONTAINER_SEARCH_HINTS.get(slug, slug.replace("-", " "))
        chosen_emojis = emojis or TOPIC_EMOJIS.get(categoria, "🔥✨")

        direct_items = self._try_direct_container_scrape(container_url, limit=limit)
        if direct_items:
            return [
                ProdutoOferta(
                    **item,
                    categoria=categoria,
                    emojis=chosen_emojis,
                    loja="mercado_livre",
                )
                for item in direct_items[:limit]
            ]

        return self.buscar_ofertas(
            query=search_hint,
            categoria=categoria,
            emojis=chosen_emojis,
            limit=limit,
        )

    def resolver_link_direto_produto(self, titulo: str, fallback_url: str) -> str:
        """
        Tenta resolver o permalink direto da página do produto no Mercado Livre (`/p/MLB...` ou `/MLB-...`).
        Caso não encontre permalink individual, retorna o link específico do produto (`fallback_url`),
        nunca sobrescrevendo com link genérico de container.
        """
        if "/p/MLB" in fallback_url or "/MLB-" in fallback_url or "/up/MLB" in fallback_url:
            return fallback_url

        ascii_title = (
            unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode("ascii")
        )
        tokens = re.findall(r"[a-zA-Z0-9]+", ascii_title)
        candidate_phrases: list[str] = []
        for n_words in (4, 3):
            phrase = " ".join(tokens[:n_words])
            if phrase and phrase not in candidate_phrases:
                candidate_phrases.append(phrase)

        for phrase in candidate_phrases:
            query = f"site:mercadolivre.com.br/p/MLB {phrase}"
            try:
                resp = requests.get(
                    "https://www.bing.com/search",
                    params={"q": query, "setlang": "pt-BR", "cc": "BR"},
                    headers={"User-Agent": BROWSER_USER_AGENT},
                    timeout=8,
                )
                if resp.status_code == 200:
                    text = unescape(resp.text)
                    b64_matches = re.findall(r"[?&]u=a1([A-Za-z0-9\-_]+)", text)
                    for b64_str in b64_matches:
                        padded = b64_str + "=" * (-len(b64_str) % 4)
                        try:
                            decoded_url = (
                                base64.urlsafe_b64decode(padded)
                                .decode("utf-8", errors="ignore")
                                .split("?")[0]
                            )
                            if decoded_url.startswith("http") and (
                                "/p/MLB" in decoded_url
                                or "/MLB-" in decoded_url
                                or "/up/MLB" in decoded_url
                            ):
                                return decoded_url
                        except Exception:
                            continue
            except Exception as exc:
                logger.debug("Aviso ao resolver permalink direto para '%s': %s", titulo, exc)

        return fallback_url

    def extrair_todos_containers_mapeados(self) -> list[dict[str, Any]]:
        """Itera por todos os tópicos e seus respectivos links de containers do Mercado Livre."""
        ofertas_extraidas: list[dict[str, Any]] = []
        for topico, urls in ML_TOPIC_CONTAINERS.items():
            for url in urls:
                try:
                    item = self.extrair_de_container(
                        container_url=url,
                        categoria=topico,
                        emojis=TOPIC_EMOJIS.get(topico, "🔥✨"),
                    )
                    ofertas_extraidas.append(item)
                except Exception as exc:
                    logger.error(
                        "Erro ao extrair container %s do tópico %s: %s",
                        url,
                        topico,
                        exc,
                    )
        return ofertas_extraidas

    def _try_direct_container_scrape(
        self,
        container_url: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Tenta fazer o parse direto dos cards HTML do container caso não caia em challenge."""
        clean_url = container_url.split("#")[0]
        encontrados: list[dict[str, Any]] = []
        try:
            resp = self.session.get(clean_url, timeout=12)
            if resp.status_code != 200 or "suspicious-traffic" in resp.text:
                return []

            cards = re.findall(
                r'<li[^>]*class="[^"]*ui-search-layout__item[^"]*"[^>]*>(.*?)</li>',
                resp.text,
                flags=re.DOTALL,
            )
            for card in cards:
                title_m = re.search(
                    r'class="[^"]*poly-component__title[^"]*"[^>]*>(.*?)</a>',
                    card,
                    flags=re.DOTALL,
                )
                link_m = re.search(r'href="(https://[^"]+mercadolivre\.com\.br[^"]+)"', card)
                img_m = re.search(
                    r'(?:data-src|src)="(https://http2\.mlstatic\.com/[^"]+)"',
                    card,
                )
                prices = re.findall(
                    r"andes-money-amount__fraction[^>]*>([\d\.]+)</span>"
                    r"(?:.*?andes-money-amount__cents[^>]*>(\d+)</span>)?",
                    card,
                    flags=re.DOTALL,
                )
                if not (title_m and link_m and prices):
                    continue

                vals: list[float] = []
                for frac, cents in prices:
                    vals.append(float(frac.replace(".", "") + ("." + cents if cents else ".00")))

                tem_desconto = len(vals) >= 2 and vals[0] > vals[1]
                promo = vals[1] if tem_desconto else vals[0]
                orig = vals[0] if tem_desconto else None
                clean_link = unescape(link_m.group(1)).split("#")[0]
                mlb_m = re.search(r"(MLB-?\d+)", clean_link, flags=re.IGNORECASE)

                rating_m = re.search(
                    r'poly-reviews__rating[^>]*>([\d,\.]+)<',
                    card,
                )

                encontrados.append(
                    {
                        "id": mlb_m.group(1).upper().replace("-", "") if mlb_m else "",
                        "titulo": re.sub(r"<[^>]+>", "", title_m.group(1)).strip(),
                        "preco_original": orig,
                        "preco_promocional": promo,
                        "link_produto": clean_link,
                        "imagem_url": unescape(img_m.group(1)) if img_m else None,
                        "avaliacao": float(rating_m.group(1).replace(",", ".")) if rating_m else None,
                    }
                )
                if len(encontrados) >= limit:
                    break
        except Exception as exc:
            logger.debug("Scrape direto do container %s indisponível: %s", clean_url, exc)
        return encontrados

    def _fetch_list_from_public_api(
        self,
        query: str,
        limit: int = 20,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        """Consulta https://api.mercadolibre.com/sites/MLB/search?q= buscando até `limit` itens com paginação."""
        try:
            offset = max(0, page) * limit
            response = self.session.get(
                self.SEARCH_API_URL,
                params={"q": query, "limit": limit, "offset": offset},
                timeout=15,
            )
            if response.status_code != 200:
                return []

            results: list[dict[str, Any]] = response.json().get("results") or []

            items_out: list[dict[str, Any]] = []
            for item in results[:limit]:
                promo_price = float(item.get("price") or 0)
                if promo_price <= 0:
                    continue
                thumbnail = str(item.get("thumbnail") or "")
                high_res_img = re.sub(r"-I\.([a-zA-Z0-9]+)$", r"-O.\1", thumbnail) or thumbnail
                reviews_obj = item.get("reviews") or {}

                items_out.append(
                    {
                        "id": str(item.get("id") or ""),
                        "titulo": str(item.get("title") or query),
                        "preco_original": item.get("original_price"),
                        "preco_promocional": promo_price,
                        "link_produto": str(item.get("permalink") or ""),
                        "imagem_url": high_res_img,
                        "avaliacao": reviews_obj.get("rating_average"),
                    }
                )
            return items_out
        except Exception:
            return []
