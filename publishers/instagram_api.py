"""
Publicação no Instagram pela Graph API da Meta (login unificado, token de usuário do sistema).

Configuração no .env:
- `META_ACCESS_TOKEN`: token do usuário do sistema (não expira), com `instagram_basic` e
  `instagram_content_publish`.
- `META_IG_USER_ID`: id da conta profissional do Instagram (17841…).
- `META_MIDIA_URL`: endereço público (https) que serve os cards (`painel/midia.py` atrás do
  Tailscale Funnel). A API não recebe upload: ela baixa cada imagem dessa URL.

Fluxo: um container por card (`is_carousel_item`), o container do carrossel com a legenda, espera
cada um ficar `FINISHED` e publica. Post de um card só vira foto simples. Tudo síncrono: chame de
dentro de `asyncio.to_thread`.
"""

import hashlib
import hmac
import logging
import time
from typing import Any, Callable, Optional

import requests

from config import env

logger = logging.getLogger("diquinhas_e_promos.instagram_api")

VERSAO_PADRAO = "v26.0"
MAX_CARDS = 10  # limite do carrossel na API
ESPERA_STATUS = 3  # segundos entre consultas ao status do container
TENTATIVAS_STATUS = 40  # ~2 min por container


class ErroInstagram(Exception):
    """Falha da API da Meta, já com a mensagem pronta para o painel."""


def configurado() -> bool:
    return bool(env("META_ACCESS_TOKEN") and env("META_IG_USER_ID") and env("META_MIDIA_URL").startswith("https://"))


def assinatura(post_id: int) -> str:
    """Parte secreta da URL pública dos cards: sem ela ninguém adivinha o endereço de outro post."""
    chave = hashlib.sha256(("midia:" + env("META_ACCESS_TOKEN")).encode()).digest()
    return hmac.new(chave, str(int(post_id)).encode(), hashlib.sha256).hexdigest()[:32]


def urls_publicas(post_id: int, n_cards: int) -> list[str]:
    base = env("META_MIDIA_URL").rstrip("/")
    return [f"{base}/ig/{int(post_id)}/{assinatura(post_id)}/{n:02d}.jpg" for n in range(n_cards)]


def _base() -> str:
    return f"https://graph.facebook.com/{env('META_GRAPH_VERSION', VERSAO_PADRAO)}"


def _chamar(metodo: str, caminho: str, **params: Any) -> dict[str, Any]:
    params["access_token"] = env("META_ACCESS_TOKEN")
    try:
        resp = requests.request(metodo, f"{_base()}/{caminho}", data=params if metodo == "POST" else None,
                                params=params if metodo == "GET" else None, timeout=60)
        dados = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ErroInstagram(f"sem resposta da Meta: {exc}") from exc
    if not resp.ok or "error" in dados:
        erro = dados.get("error") or {}
        detalhe = erro.get("error_user_msg") or erro.get("message") or f"HTTP {resp.status_code}"
        raise ErroInstagram(f"Meta recusou ({erro.get('code', resp.status_code)}): {detalhe}")
    return dados


def _esperar_container(container_id: str, dormir: Callable[[float], None] = time.sleep) -> None:
    for _ in range(TENTATIVAS_STATUS):
        status = _chamar("GET", container_id, fields="status_code,status").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            detalhe = _chamar("GET", container_id, fields="status").get("status") or status
            raise ErroInstagram(f"a Meta não processou a imagem: {detalhe}")
        dormir(ESPERA_STATUS)
    raise ErroInstagram("a Meta demorou demais para processar as imagens")


def criar_container(urls: list[str], legenda: str, dormir: Callable[[float], None] = time.sleep) -> str:
    """Cria (sem publicar) o container do post e espera ficar pronto. Devolve o id do container."""
    if not urls:
        raise ErroInstagram("post sem imagens")
    if len(urls) > MAX_CARDS:
        raise ErroInstagram(f"o carrossel aceita no máximo {MAX_CARDS} imagens")
    ig = env("META_IG_USER_ID")
    if len(urls) == 1:
        container = _chamar("POST", f"{ig}/media", image_url=urls[0], caption=legenda)["id"]
    else:
        filhos = [_chamar("POST", f"{ig}/media", image_url=url, is_carousel_item="true")["id"] for url in urls]
        for filho in filhos:
            _esperar_container(filho, dormir)
        container = _chamar("POST", f"{ig}/media", media_type="CAROUSEL", children=",".join(filhos),
                            caption=legenda)["id"]
    _esperar_container(container, dormir)
    return container


def publicar(urls: list[str], legenda: str, dormir: Callable[[float], None] = time.sleep) -> str:
    """Publica o post (carrossel ou foto) e devolve o id da mídia no Instagram."""
    container = criar_container(urls, legenda, dormir)
    media_id = _chamar("POST", f"{env('META_IG_USER_ID')}/media_publish", creation_id=container)["id"]
    logger.info("Post publicado no Instagram: mídia %s (%d imagens).", media_id, len(urls))
    return media_id


def link_do_post(media_id: str) -> Optional[str]:
    try:
        return _chamar("GET", media_id, fields="permalink").get("permalink")
    except ErroInstagram:
        return None
