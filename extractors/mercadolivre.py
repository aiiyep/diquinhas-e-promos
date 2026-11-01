import base64
from html import unescape
import logging
import re
import time
from typing import Any, Callable, Optional
import unicodedata

import requests

from config import BROWSER_HEADERS, BROWSER_USER_AGENT, env
from extractors.filtros import eh_produto_masculino, genero_permitido
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
    "ofertas": "🔥💸✨",
}

# Subcategorias de Beleza e Cuidado Pessoal (MLB1246) usadas no tópico Ofertas.
# Ficam de fora: Barbearia, Higiene Pessoal, Farmácia, Outros e Artigos para Cabeleireiros.
OFERTAS_SUBCATEGORIAS: dict[str, str] = {
    "MLB199407": "Cuidados com a Pele",
    "MLB1263": "Cuidados com o Cabelo",
    "MLB1248": "Maquiagem",
    "MLB29884": "Manicure e Pedicure",
    "MLB6284": "Perfumes",
    "MLB278194": "Tratamentos de Beleza",
    "MLB5383": "Depilação",
    "MLB455174": "Artefatos para Cabelo",
}

# Em Perfumes os mais vendidos visíveis pela API são quase todos masculinos (os demais são
# USER_PRODUCT, que a API não deixa ler). Por isso essa subcategoria busca perfumes femininos conhecidos.
PERFUMES_CATEGORIA = "MLB6284"
PERFUMES_DOMINIOS = {"MLB-PERFUMES"}
PERFUMES_FEMININOS_TERMOS: list[str] = [
    "Floratta Boticario",
    "Egeo Dolce",
    "Lily Boticario",
    "Natura Una",
    "Natura Ekos Frescor",
    "Kaiak Feminino",
    "Essencial Feminino Natura",
    "Glamour Boticario",
    "Cuide-se Bem Boticario",
    "Body Splash Feminino",
    "Deo Colonia Feminina",
    "La Vie Est Belle Lancome",
    "Good Girl Carolina Herrera",
    "212 VIP Rose",
    "Olympea Paco Rabanne",
    "Perfume Arabe Feminino",
]

API_URL = "https://api.mercadolibre.com"

# Caches em memória (compartilhados entre instâncias): {chave: (expira_em, dados)}
CACHE_DESTAQUES_TTL = 6 * 3600
CACHE_PRODUTO_TTL = 24 * 3600
_CACHE_DESTAQUES: dict[str, tuple[float, Any]] = {}
_CACHE_PRODUTOS: dict[str, tuple[float, Any]] = {}

__all__ = [
    "CONTAINER_SEARCH_HINTS",
    "ML_TOPIC_CONTAINERS",
    "MercadoLivreExtractor",
    "OFERTAS_SUBCATEGORIAS",
    "PERFUMES_CATEGORIA",
    "PERFUMES_DOMINIOS",
    "PERFUMES_FEMININOS_TERMOS",
    "ProdutoOferta",
    "TOPIC_EMOJIS",
]


def _api_get(
    url: str,
    headers: dict[str, str],
    params: Optional[dict[str, Any]] = None,
    timeout: int = 15,
    tentativas: int = 3,
) -> Optional[requests.Response]:
    """
    GET na API oficial que respeita o limite de requisições: ao receber HTTP 429, espera
    (o `Retry-After` informado ou 2s, 4s, 8s…, no máximo 30s) e tenta de novo.
    """
    resp: Optional[requests.Response] = None
    for tentativa in range(tentativas):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except Exception as exc:
            logger.debug("Erro de rede em %s: %s", url, exc)
            return None
        if resp.status_code != 429:
            return resp
        try:
            espera = float(resp.headers.get("Retry-After") or 0) or 2.0 ** (tentativa + 1)
        except ValueError:
            espera = 2.0 ** (tentativa + 1)
        espera = min(espera, 30.0)
        logger.warning("Limite de requisições da API do ML (HTTP 429); aguardando %.0fs...", espera)
        time.sleep(espera)
    return resp


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
            resp = _api_get(
                f"{API_URL}/products/search",
                headers,
                params={
                    "status": "active",
                    "site_id": "MLB",
                    "q": query,
                    "limit": limit,
                    "offset": max(0, page) * limit,
                },
            )
            if resp is None or resp.status_code != 200:
                logger.warning(
                    "Catálogo oficial ML retornou HTTP %s para '%s'.", getattr(resp, "status_code", "erro"), query
                )
                return []

            for prod in resp.json().get("results") or []:
                prod_id = str(prod.get("id") or "")
                nome = str(prod.get("name") or "").strip()
                pictures = prod.get("pictures") or []
                imagem = str(pictures[0].get("url") or "") if pictures else ""
                if not prod_id or not nome or not imagem:
                    continue

                melhor = self._melhor_anuncio(prod_id, headers)
                if not melhor:
                    continue  # produto sem anúncio ativo (indisponível)

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

    @staticmethod
    def _melhor_anuncio(prod_id: str, headers: dict[str, str]) -> Optional[dict[str, Any]]:
        """Anúncio ativo de menor preço de um produto de catálogo (`/products/{id}/items`)."""
        resp = _api_get(f"{API_URL}/products/{prod_id}/items", headers, timeout=10)
        if resp is None or resp.status_code != 200:
            return None
        try:
            anuncios = [a for a in (resp.json().get("results") or []) if float(a.get("price") or 0) > 0]
        except Exception:
            return None
        return min(anuncios, key=lambda a: float(a["price"])) if anuncios else None

    def _headers_oficiais(self) -> Optional[dict[str, str]]:
        token = self._obter_token_oficial()
        if not token:
            logger.error("Ofertas exigem ML_CLIENT_ID/ML_CLIENT_SECRET configurados no .env.")
            return None
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    @staticmethod
    def _cache_get(cache: dict[str, tuple[float, Any]], chave: str) -> Any:
        entrada = cache.get(chave)
        return entrada[1] if entrada and entrada[0] > time.time() else None

    def _produto_catalogo(self, prod_id: str, headers: dict[str, str]) -> Optional[dict[str, Any]]:
        """Dados do produto de catálogo (nome, fotos, atributos), guardados em cache por 24h."""
        prod = self._cache_get(_CACHE_PRODUTOS, prod_id)
        if prod is None:
            resp = _api_get(f"{API_URL}/products/{prod_id}", headers, timeout=10)
            if resp is None or resp.status_code != 200:
                return None
            prod = resp.json()
            _CACHE_PRODUTOS[prod_id] = (time.time() + CACHE_PRODUTO_TTL, prod)
        return prod

    def _montar_oferta(
        self,
        prod: dict[str, Any],
        headers: dict[str, str],
        categoria: str,
        emojis: str,
    ) -> Optional[ProdutoOferta]:
        """Transforma um produto de catálogo em oferta, se for feminino/neutro e tiver desconto real."""
        prod_id = str(prod.get("id") or "")
        nome = str(prod.get("name") or "").strip()
        pictures = prod.get("pictures") or []
        if not prod_id or not nome or not pictures:
            return None

        generos = [
            str(a.get("value_name") or "")
            for a in prod.get("attributes") or []
            if a.get("id") == "GENDER"
        ]
        if eh_produto_masculino(nome, str(prod.get("domain_id") or ""), *generos) or not genero_permitido(generos):
            logger.info("Produto masculino/fora do público descartado das ofertas: %s (gênero=%s)", nome[:60], generos)
            return None

        melhor = self._melhor_anuncio(prod_id, headers)
        if not melhor or not melhor.get("original_price"):
            return None
        oferta = ProdutoOferta(
            id=prod_id,
            titulo=nome,
            preco_original=melhor["original_price"],
            preco_promocional=float(melhor["price"]),
            link_produto=f"https://www.mercadolivre.com.br/{_slugify(nome)}/p/{prod_id}",
            imagem_url=str(pictures[0].get("url") or ""),
            categoria=categoria,
            emojis=emojis,
            loja="mercado_livre",
        )
        # o modelo zera "De" que não for maior que o preço atual
        return oferta if oferta.preco_original else None

    # Atributos do catálogo que não dizem nada sobre o produto
    _ATRIBUTOS_IGNORADOS = {"Título manual", "Origem do dado do pacote de fábrica", "Formato de venda", "É kit"}

    def detalhes_produto(self, prod_id: str, max_atributos: int = 18) -> Optional[dict[str, Any]]:
        """
        Descrição curta e atributos do produto de catálogo (cache de 24h). Usado no "Vale a pena?"
        do Instagram, para o texto se basear no que a ficha oficial diz.
        """
        headers = self._headers_oficiais()
        prod = self._produto_catalogo(prod_id, headers) if headers else None
        if not prod:
            return None
        descricao = ((prod.get("short_description") or {}).get("content") or "").strip()
        atributos = [
            f"{a.get('name')}: {a.get('value_name')}"
            for a in prod.get("attributes") or []
            if a.get("name") and a.get("value_name") and a.get("name") not in self._ATRIBUTOS_IGNORADOS
        ][:max_atributos]
        return {"descricao": descricao[:1200], "atributos": atributos}

    @staticmethod
    def _ordenar_por_desconto(ofertas: list[ProdutoOferta]) -> list[ProdutoOferta]:
        return sorted(ofertas, key=lambda o: o.preco_promocional / (o.preco_original or o.preco_promocional))

    def buscar_ofertas_mais_vendidos(
        self,
        categoria_id: str,
        categoria: str = "ofertas",
        emojis: str = "🔥💸✨",
        ignorar: Optional[Callable[[str], bool]] = None,
    ) -> list[ProdutoOferta]:
        """
        Mais vendidos de uma categoria do ML (`/highlights/MLB/category/{id}`) que estão com
        desconto real (preço "De" informado pelo próprio anúncio), sem produtos masculinos,
        ordenados do maior para o menor percentual de desconto.
        `ignorar(id)` permite pular produtos já postados antes de gastar chamadas na API.
        A lista de mais vendidos fica em cache por 6h; os preços são sempre consultados na hora.
        """
        headers = self._headers_oficiais()
        if headers is None:
            return []

        destaques = self._cache_get(_CACHE_DESTAQUES, categoria_id)
        if destaques is None:
            resp = _api_get(f"{API_URL}/highlights/MLB/category/{categoria_id}", headers)
            if resp is None or resp.status_code != 200:
                logger.warning(
                    "Mais vendidos de %s retornou HTTP %s.", categoria_id, getattr(resp, "status_code", "erro")
                )
                return []
            destaques = resp.json().get("content") or []
            _CACHE_DESTAQUES[categoria_id] = (time.time() + CACHE_DESTAQUES_TTL, destaques)

        ofertas: list[ProdutoOferta] = []
        for destaque in destaques:
            prod_id = str(destaque.get("id") or "")
            # USER_PRODUCT (MLBU...) só pode ser lido pelo vendedor dono; a API responde 403
            if destaque.get("type") != "PRODUCT" or not prod_id:
                continue
            if ignorar and ignorar(prod_id):
                continue
            prod = self._produto_catalogo(prod_id, headers)
            if prod:
                oferta = self._montar_oferta({**prod, "id": prod_id}, headers, categoria, emojis)
                if oferta:
                    ofertas.append(oferta)

        logger.info("Mais vendidos de %s com desconto real: %d.", categoria_id, len(ofertas))
        return self._ordenar_por_desconto(ofertas)

    def buscar_ofertas_por_termos(
        self,
        termos: list[str],
        categoria: str = "ofertas",
        emojis: str = "🔥💸✨",
        ignorar: Optional[Callable[[str], bool]] = None,
        limite_por_termo: int = 10,
        dominios: Optional[set[str]] = None,
    ) -> list[ProdutoOferta]:
        """
        Busca no catálogo (`/products/search`) produtos específicos, como os perfumes femininos
        de `PERFUMES_FEMININOS_TERMOS`, e devolve os que têm desconto real e gênero permitido,
        do maior para o menor desconto. O resultado de cada busca fica em cache por 6h.
        `dominios` restringe ao tipo de produto do catálogo (ex.: só `MLB-PERFUMES`).
        """
        headers = self._headers_oficiais()
        if headers is None:
            return []

        ofertas: list[ProdutoOferta] = []
        vistos: set[str] = set()
        for termo in termos:
            chave = f"q:{termo.lower()}"
            resultados = self._cache_get(_CACHE_DESTAQUES, chave)
            if resultados is None:
                resp = _api_get(
                    f"{API_URL}/products/search",
                    headers,
                    params={"status": "active", "site_id": "MLB", "q": termo, "limit": limite_por_termo},
                )
                if resp is None or resp.status_code != 200:
                    logger.warning(
                        "Busca de catálogo por '%s' retornou HTTP %s.", termo, getattr(resp, "status_code", "erro")
                    )
                    continue
                resultados = resp.json().get("results") or []
                _CACHE_DESTAQUES[chave] = (time.time() + CACHE_DESTAQUES_TTL, resultados)

            for prod in resultados:
                prod_id = str(prod.get("id") or "")
                if not prod_id or prod_id in vistos or (ignorar and ignorar(prod_id)):
                    continue
                if dominios and prod.get("domain_id") not in dominios:
                    continue
                vistos.add(prod_id)
                oferta = self._montar_oferta(prod, headers, categoria, emojis)
                if oferta:
                    ofertas.append(oferta)

        logger.info("Busca por %d termos com desconto real: %d.", len(termos), len(ofertas))
        return self._ordenar_por_desconto(ofertas)

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
