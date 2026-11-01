"""
Carrosséis do Instagram: 3 por dia, cada horário com um formato.

    11h30  Achado do dia      capa "destaque", 1 a 3 produtos
    17h30  Abaixo de R$ 30/50 capa "lista",    3 a 5 produtos
    21h00  Top do dia         capa "grade",    3 a 5 produtos

Uma hora antes de cada horário o bot prepara o carrossel (`preparar_post`): pega o que já foi
postado no Telegram nas últimas 72h, confere o preço de novo na loja, monta os cards e a legenda e
deixa o post "pendente" no painel. No horário (`publicar_do_horario`), o post aprovado é publicado;
o que não foi aprovado expira, a menos que `INSTAGRAM_AUTO=1`. Um produto não volta ao Instagram
por 14 dias. Horários em `INSTAGRAM_HORARIOS` (ex.: "11:30,17:30,21:00", na ordem dos formatos).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import shutil
from typing import Any, Iterable, Optional
import uuid
from zoneinfo import ZoneInfo

import requests

from config import env
from database.historico_ofertas import HistoricoDB
from extractors.filtros import eh_produto_masculino
from extractors.mercadolivre import MercadoLivreExtractor
from extractors.shopee import ShopeeExtractor
from publishers.instagram_cards import PALETAS, ProdutoCard, montar_carrossel, salvar_jpeg
from publishers.instagram_legenda import FORMATOS, gerar_textos

logger = logging.getLogger("diquinhas_e_promos.instagram")

_TZ = ZoneInfo("America/Sao_Paulo")
PASTA_CARDS = Path(__file__).resolve().parent.parent / "data" / "instagram"
ANTECEDENCIA = timedelta(hours=1)
HORAS_CANDIDATOS = 72
DIAS_SEM_REPETIR = 14
DIAS_GUARDAR_CARDS = 10
PRE_SELECAO = 24  # quantos candidatos têm o preço reconsultado por carrossel


@dataclass(frozen=True)
class Slot:
    chave: str  # = formato
    horario_padrao: str
    estilo_capa: str
    minimo: int
    maximo: int


SLOTS: tuple[Slot, ...] = (
    Slot("achado", "11:30", "destaque", 1, 3),
    Slot("barato", "17:30", "lista", 3, 5),
    Slot("top5", "21:00", "grade", 3, 5),
)
_SLOTS = {s.chave: s for s in SLOTS}


def instagram_ativo() -> bool:
    return env("INSTAGRAM_ATIVO", "1") != "0"


def publicacao_automatica() -> bool:
    return env("INSTAGRAM_AUTO", "0") == "1"


def horarios() -> list[tuple[Slot, int, int]]:
    """(slot, hora, minuto) de publicação de cada formato, lendo `INSTAGRAM_HORARIOS` se houver."""
    configurados = [h.strip() for h in env("INSTAGRAM_HORARIOS").split(",") if h.strip()]
    resultado = []
    for i, slot in enumerate(SLOTS):
        texto = configurados[i] if i < len(configurados) else slot.horario_padrao
        try:
            hora, minuto = (int(x) for x in texto.split(":"))
            assert 0 <= hora < 24 and 0 <= minuto < 60
        except (ValueError, AssertionError):
            logger.warning("INSTAGRAM_HORARIOS inválido (%r); usando %s.", texto, slot.horario_padrao)
            hora, minuto = (int(x) for x in slot.horario_padrao.split(":"))
        resultado.append((slot, hora, minuto))
    return resultado


def agendado_hoje(slot_chave: str, agora: Optional[datetime] = None) -> datetime:
    """Horário de publicação do formato no dia de `agora` (São Paulo), sem fuso."""
    agora = (agora or datetime.now(_TZ)).astimezone(_TZ)
    for slot, hora, minuto in horarios():
        if slot.chave == slot_chave:
            return agora.replace(hour=hora, minute=minuto, second=0, microsecond=0, tzinfo=None)
    raise KeyError(slot_chave)


# ---------------------------------------------------------------- produtos


def desconto_pct(preco: Optional[float], original: Optional[float]) -> int:
    if preco and original and original > preco:
        return round((1 - preco / original) * 100)
    return 0


def _com_desconto(item: dict[str, Any]) -> dict[str, Any]:
    original = item.get("preco_original")
    if not original or original <= item["preco"]:
        original = None
    return {**item, "preco_original": original, "desconto": desconto_pct(item["preco"], original) or None}


def selecionar(slot_chave: str, candidatos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Optional[int]]:
    """Escolhe e ordena os produtos do carrossel. Devolve (produtos, limite de preço do formato)."""
    slot = _SLOTS[slot_chave]
    # O histórico só tem o que passou no filtro do Telegram, mas o Instagram confere de novo
    itens = [_com_desconto(c) for c in candidatos if c.get("preco") and not eh_produto_masculino(c.get("titulo") or "")]
    chave_desc = lambda p: (p["desconto"] or 0, p.get("avaliacao") or 0)  # noqa: E731
    limite = None

    if slot_chave == "achado":
        escolhidos = sorted(itens, key=chave_desc, reverse=True)
        if escolhidos and (escolhidos[0]["desconto"] or 0) < 15:
            # sem um desconto que valha o "achado", a estrela é o produto mais bem avaliado
            escolhidos.sort(key=lambda p: (p.get("avaliacao") or 0, p["desconto"] or 0), reverse=True)
    elif slot_chave == "barato":
        limite = 30 if sum(p["preco"] <= 30 for p in itens) >= slot.minimo else 50
        escolhidos = sorted(
            (p for p in itens if p["preco"] <= limite), key=lambda p: (-(p["desconto"] or 0), p["preco"])
        )
    else:  # top5
        escolhidos = sorted((p for p in itens if p["desconto"]), key=chave_desc, reverse=True)

    escolhidos = _sem_titulos_repetidos(escolhidos)[: slot.maximo]
    return (escolhidos if len(escolhidos) >= slot.minimo else []), limite


def _palavras(titulo: str) -> set[str]:
    return {p for p in re.findall(r"\w+", titulo.lower()) if len(p) > 2}


def parecidos(titulo_a: str, titulo_b: str) -> bool:
    """Mesmo produto com outro título (lojas diferentes, palavras trocadas de lugar)."""
    a, b = _palavras(titulo_a), _palavras(titulo_b)
    comuns = len(a & b)
    return comuns >= 3 and comuns >= 0.6 * min(len(a), len(b))


def _sem_titulos_repetidos(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    saida: list[dict[str, Any]] = []
    for item in itens:
        if not any(parecidos(str(item.get("titulo") or ""), str(o.get("titulo") or "")) for o in saida):
            saida.append(item)
    return saida


def _pre_selecionar(slot_chave: str, candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Com o preço gravado no histórico, separa os candidatos mais promissores para reconsultar."""
    itens = [_com_desconto(c) for c in candidatos if c.get("preco")]
    if slot_chave == "barato":
        itens = [p for p in itens if p["preco"] <= 60]
        itens.sort(key=lambda p: (-(p["desconto"] or 0), p["preco"]))
    else:
        itens.sort(key=lambda p: (p["desconto"] or 0, p.get("avaliacao") or 0), reverse=True)
    return itens[:PRE_SELECAO]


def atualizar_precos(candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Reconsulta preço e "De" na loja (Shopee: Open API de Afiliados; ML: anúncio de menor preço do
    produto de catálogo). Quem não puder ser conferido fica de fora: o card nunca mostra preço velho.
    """
    atualizados: list[dict[str, Any]] = []

    shopee = [c for c in candidatos if str(c["id_produto"]).startswith("SHOPEE")]
    if shopee:
        ext = ShopeeExtractor()
        entrada = [
            {
                "id": c["id_produto"], "titulo": c["titulo"] or "", "preco_promocional": c["preco"],
                "link_produto": c.get("link") or "https://shopee.com.br", "imagem_url": c.get("imagem_url"),
                "categoria": "instagram", "emojis": "", "loja": "shopee",
            }
            for c in shopee
        ]
        if ext._has_valid_graphql_credentials():
            por_id = {o["id"]: o for o in ext.atualizar_com_api(entrada)}
            for c in shopee:
                novo = por_id.get(c["id_produto"])
                if novo:
                    atualizados.append({
                        **c, "preco": novo["preco_promocional"], "preco_original": novo.get("preco_original"),
                        "avaliacao": novo.get("avaliacao"), "imagem_url": novo.get("imagem_url") or c.get("imagem_url"),
                    })
        else:
            logger.warning("Sem credenciais da Shopee: produtos da Shopee ficam fora do Instagram.")

    ml = [c for c in candidatos if str(c["id_produto"]).startswith("MLB")]
    if ml:
        ext_ml = MercadoLivreExtractor()
        headers = ext_ml._headers_oficiais()
        for c in ml if headers else []:
            anuncio = ext_ml._melhor_anuncio(c["id_produto"], headers)
            if anuncio and float(anuncio.get("price") or 0) > 0:
                atualizados.append({
                    **c, "preco": float(anuncio["price"]),
                    "preco_original": float(anuncio["original_price"]) if anuncio.get("original_price") else None,
                })
    return atualizados


def _baixar_foto(url: str) -> Optional[bytes]:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
            return resp.content
    except requests.RequestException as exc:
        logger.warning("Foto não baixada (%s): %s", url[:80], exc)
    return None


# ---------------------------------------------------------------- preparo e publicação


def preparar_post(
    db: HistoricoDB,
    slot_chave: str,
    agendado_para: datetime,
    excluir: Iterable[str] = (),
    agora: Optional[datetime] = None,
) -> Optional[int]:
    """Monta o carrossel do horário e o grava como pendente. Devolve o id, ou None se faltou produto."""
    slot = _SLOTS[slot_chave]
    agora = (agora or datetime.now(_TZ)).astimezone(_TZ)
    usados = db.produtos_usados_instagram(DIAS_SEM_REPETIR)
    bloqueados = set(usados) | set(excluir)
    candidatos = [
        c for c in db.candidatos_instagram(HORAS_CANDIDATOS)
        if c["id_produto"] not in bloqueados
        and not any(parecidos(c.get("titulo") or "", t) for t in usados.values())
    ]

    produtos, limite = selecionar(slot_chave, atualizar_precos(_pre_selecionar(slot_chave, candidatos)))

    cards_produtos: list[ProdutoCard] = []
    escolhidos: list[dict[str, Any]] = []
    for p in produtos:
        foto = _baixar_foto(p["imagem_url"])
        if foto is None:
            continue
        card = ProdutoCard.de_dict(p, foto)
        if card.foto is None:
            continue
        cards_produtos.append(card)
        escolhidos.append(p)

    if len(escolhidos) < slot.minimo:
        motivo = f"{FORMATOS[slot_chave].nome}: faltaram produtos inéditos com preço conferido ({len(escolhidos)})"
        logger.warning("Carrossel do Instagram não montado. %s", motivo)
        db.registrar_disparo("Instagram", False, motivo)
        return None

    textos = gerar_textos(slot_chave, escolhidos, agora, limite)
    for card, nome in zip(cards_produtos, textos.nomes):
        card.titulo = nome

    indice = [s.chave for s in SLOTS].index(slot_chave)
    variante = agendado_para.toordinal() * len(SLOTS) + indice
    imagens = montar_carrossel(
        cards_produtos, textos.titulo_capa, textos.subtitulo, variante=variante,
        estilo=slot.estilo_capa, dia=agendado_para.date(),
    )

    pasta = PASTA_CARDS / f"{agendado_para:%Y%m%d-%H%M}-{slot_chave}-{uuid.uuid4().hex[:6]}"
    pasta.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(imagens):
        salvar_jpeg(img, pasta / f"{i:02d}.jpg")

    resumo = [
        {k: p.get(k) for k in ("id_produto", "titulo", "preco", "preco_original", "desconto", "link", "loja")}
        | {"id": p["id_produto"], "nome": nome}
        for p, nome in zip(escolhidos, textos.nomes)
    ]
    post_id = db.criar_post_instagram(
        slot_chave, agendado_para, textos.titulo_capa, textos.legenda, resumo, str(pasta), len(imagens), textos.usou_ia
    )
    db.registrar_disparo(
        "Instagram", True,
        f"Carrossel '{textos.titulo_capa}' ({len(escolhidos)} produtos, paleta {PALETAS[variante % len(PALETAS)].nome}) "
        f"aguardando aprovação para {agendado_para:%H:%M}",
    )
    limpar_cards_antigos()
    return post_id


def regerar_post(db: HistoricoDB, post_id: int) -> Optional[int]:
    """'Gerar outro': monta um carrossel novo para o mesmo horário, sem os produtos do anterior."""
    antigo = db.post_instagram(post_id)
    if not antigo or antigo["status"] not in ("pendente", "aprovado", "descartado", "expirado", "falhou"):
        return None
    excluir = {p["id"] for p in antigo["produtos"]}
    agendado = datetime.fromisoformat(antigo["agendado_para"])
    novo = preparar_post(db, antigo["slot"], agendado, excluir=excluir)
    if novo:
        db.atualizar_post_instagram(post_id, "substituido")
    return novo


def publicar_post(db: HistoricoDB, post: dict[str, Any]) -> bool:
    """
    Publica o carrossel no Instagram (e na Página do Facebook). Ainda não configurado: a API exige
    as imagens num endereço público e o token da Meta. Até lá, o post aprovado fica no painel para
    ser publicado à mão (Compartilhar + Copiar legenda) e marcado como publicado.
    """
    logger.warning(
        "Carrossel %s aprovado, mas a publicação automática no Instagram ainda não está configurada; "
        "publique pelo painel.", post["id"],
    )
    db.registrar_disparo("Instagram", False, f"'{post['titulo']}' aprovado: publique pelo painel (API ainda não configurada)")
    return False


def publicar_do_horario(db: HistoricoDB, slot_chave: str, agora: Optional[datetime] = None) -> None:
    agendado = agendado_hoje(slot_chave, agora)
    for post in db.posts_instagram_do_horario(slot_chave, agendado):
        if post["status"] == "aprovado" or (post["status"] == "pendente" and publicacao_automatica()):
            publicar_post(db, post)
        elif post["status"] == "pendente":
            db.atualizar_post_instagram(post["id"], "expirado")
            db.registrar_disparo("Instagram", False, f"'{post['titulo']}' não foi aprovado a tempo e expirou")


def preparar_do_horario(db: HistoricoDB, slot_chave: str, agora: Optional[datetime] = None) -> Optional[int]:
    agendado = agendado_hoje(slot_chave, agora)
    ativos = [p for p in db.posts_instagram_do_horario(slot_chave, agendado) if p["status"] in ("pendente", "aprovado")]
    if ativos:
        logger.info("Carrossel das %s já preparado (id %s).", f"{agendado:%H:%M}", ativos[0]["id"])
        return ativos[0]["id"]
    return preparar_post(db, slot_chave, agendado, agora=agora)


def limpar_cards_antigos(dias: int = DIAS_GUARDAR_CARDS) -> None:
    if not PASTA_CARDS.exists():
        return
    corte = datetime.now(_TZ).replace(tzinfo=None) - timedelta(days=dias)
    for pasta in PASTA_CARDS.iterdir():
        try:
            if pasta.is_dir() and datetime.strptime(pasta.name[:13], "%Y%m%d-%H%M") < corte:
                shutil.rmtree(pasta, ignore_errors=True)
        except ValueError:
            continue
