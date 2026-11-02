"""
Posts do Instagram: 3 por dia, cada horário com um formato.

    11h30  Vale a pena?   análise de 1 produto (o que é, pontos fortes, fica de olho, pra quem é,
                          veredito). Se nenhum produto render uma análise, cai para o "Achado do dia".
    17h30  Dica           carrossel educativo para salvar (erros, passo a passo, mito ou verdade,
                          dicas), com até 2 achadinhos do histórico que combinem. Sem Gemini, vira
                          "Abaixo de R$ 30/50".
    21h00  Top do dia     carrossel com os maiores descontos, capa "grade", 3 a 5 produtos

Uma hora antes de cada horário o bot prepara o post (`preparar_post`): pega o que já foi postado
no Telegram nas últimas 72h, confere o preço de novo na loja, monta os cards e a legenda e deixa
o post "pendente" no painel. 30 min antes do horário (`aviso_do_horario`), se ainda estiver pendente,
o bot avisa no privado do Telegram. No horário (`publicar_do_horario`), só o post aprovado é
publicado pela API da Meta (`publishers/instagram_api.py`); o que não foi aprovado expira. Se a API
não estiver configurada ou a Meta recusar, o bot manda a capa e a legenda no privado para a
publicação à mão. Não existe publicação sem aprovação. Um produto não volta ao Instagram por 14
dias. Horários em `INSTAGRAM_HORARIOS` (ex.: "11:30,17:30,21:00", na ordem acima).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
import logging
from pathlib import Path
import re
import shutil
import unicodedata
from typing import Any, Iterable, Optional
import uuid
from zoneinfo import ZoneInfo

from PIL import Image
import requests

from config import env
from database.historico_ofertas import HistoricoDB
from extractors.filtros import eh_produto_masculino
from extractors.mercadolivre import MercadoLivreExtractor
from extractors.shopee import ShopeeExtractor
from publishers import instagram_api
from publishers.instagram_cards import (
    PALETAS,
    ProdutoCard,
    montar_carrossel,
    montar_dica,
    montar_vale_a_pena,
    salvar_jpeg,
)
from publishers.instagram_legenda import (
    FORMATOS,
    gerar_dica,
    gerar_textos,
    gerar_vale_a_pena,
    legenda_dica,
    nome_padrao,
    tema_do_dia,
)

logger = logging.getLogger("diquinhas_e_promos.instagram")

_TZ = ZoneInfo("America/Sao_Paulo")
PASTA_CARDS = Path(__file__).resolve().parent.parent / "data" / "instagram"
ANTECEDENCIA = timedelta(hours=1)  # preparo
AVISO_ANTES = timedelta(minutes=30)  # aviso no Telegram se ainda estiver pendente
HORAS_CANDIDATOS = 72
DIAS_SEM_REPETIR = 14
DIAS_GUARDAR_CARDS = 10
LIMITE_MENSAGEM_TELEGRAM = 4096
PRE_SELECAO = 24  # quantos candidatos têm o preço reconsultado por post
TENTATIVAS_VALE_A_PENA = 4  # produtos testados até um render uma análise


@dataclass(frozen=True)
class Slot:
    chave: str
    horario_padrao: str


SLOTS: tuple[Slot, ...] = (
    Slot("valeapena", "11:30"),
    Slot("dica", "17:30"),
    Slot("top5", "21:00"),
)


@dataclass(frozen=True)
class FormatoLista:
    """Carrossel de lista de produtos: estilo da capa e quantos produtos."""

    estilo_capa: str
    minimo: int
    maximo: int


LISTAS: dict[str, FormatoLista] = {
    "achado": FormatoLista("destaque", 1, 3),  # reserva do "Vale a pena?"
    "barato": FormatoLista("lista", 3, 5),
    "top5": FormatoLista("grade", 3, 5),
}


def instagram_ativo() -> bool:
    return env("INSTAGRAM_ATIVO", "1") != "0"


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


def _variante(slot_chave: str, agendado_para: datetime) -> int:
    """Paleta do post: muda a cada post (3 por dia, 5 paletas)."""
    indice = [s.chave for s in SLOTS].index(slot_chave) if slot_chave in [s.chave for s in SLOTS] else 0
    return agendado_para.toordinal() * len(SLOTS) + indice


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


def _validos(candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # O histórico só tem o que passou no filtro do Telegram, mas o Instagram confere de novo
    return [_com_desconto(c) for c in candidatos if c.get("preco") and not eh_produto_masculino(c.get("titulo") or "")]


def selecionar(formato: str, candidatos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Optional[int]]:
    """Escolhe e ordena os produtos de um carrossel de lista. Devolve (produtos, limite de preço)."""
    lista = LISTAS[formato]
    itens = _validos(candidatos)
    chave_desc = lambda p: (p["desconto"] or 0, p.get("avaliacao") or 0)  # noqa: E731
    limite = None

    if formato == "achado":
        escolhidos = sorted(itens, key=chave_desc, reverse=True)
        if escolhidos and (escolhidos[0]["desconto"] or 0) < 15:
            # sem um desconto que valha o "achado", a estrela é o produto mais bem avaliado
            escolhidos.sort(key=lambda p: (p.get("avaliacao") or 0, p["desconto"] or 0), reverse=True)
    elif formato == "barato":
        limite = 30 if sum(p["preco"] <= 30 for p in itens) >= lista.minimo else 50
        escolhidos = sorted(
            (p for p in itens if p["preco"] <= limite), key=lambda p: (-(p["desconto"] or 0), p["preco"])
        )
    else:  # top5
        escolhidos = sorted((p for p in itens if p["desconto"]), key=chave_desc, reverse=True)

    escolhidos = _sem_titulos_repetidos(escolhidos)[: lista.maximo]
    return (escolhidos if len(escolhidos) >= lista.minimo else []), limite


def ordenar_vale_a_pena(candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Candidatos ao "Vale a pena?", do melhor para o pior: primeiro os que têm prova (nota 4,5+,
    500+ vendas ou ficha do ML), e entre eles os do ML, cuja ficha (descrição e atributos) rende
    uma análise mais rica; depois os com desconto de 15%+.
    """
    def chave(p: dict[str, Any]) -> tuple:
        ficha = str(p["id_produto"]).startswith("MLB")
        prova = ficha or (p.get("avaliacao") or 0) >= 4.5 or (p.get("vendas") or 0) >= 500
        return (prova, ficha, (p["desconto"] or 0) >= 15, p.get("avaliacao") or 0, p["desconto"] or 0, p.get("vendas") or 0)

    return _sem_titulos_repetidos(sorted(_validos(candidatos), key=chave, reverse=True))


def _normalizar(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()


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


def _pre_selecionar(formato: str, candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Com o preço gravado no histórico, separa os candidatos mais promissores para reconsultar."""
    itens = [_com_desconto(c) for c in candidatos if c.get("preco")]
    if formato == "barato":
        itens = [p for p in itens if p["preco"] <= 60]
        itens.sort(key=lambda p: (-(p["desconto"] or 0), p["preco"]))
    else:
        itens.sort(key=lambda p: (p["desconto"] or 0, p.get("avaliacao") or 0), reverse=True)
    return itens[:PRE_SELECAO]


def atualizar_precos(candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Reconsulta preço e "De" na loja (Shopee: Open API de Afiliados, que também traz nota, vendas e
    loja; ML: anúncio de menor preço do produto de catálogo). Quem não puder ser conferido fica de
    fora: o card nunca mostra preço velho.
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
                        "vendas": novo.get("vendas"), "loja_nome": novo.get("loja_nome"),
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


def detalhes_produto(produto: dict[str, Any]) -> dict[str, Any]:
    """Descrição e ficha técnica (só o ML entrega pela API; a Shopee manda nota, vendas e loja)."""
    if str(produto["id_produto"]).startswith("MLB"):
        try:
            return MercadoLivreExtractor().detalhes_produto(produto["id_produto"]) or {}
        except Exception as exc:
            logger.warning("Ficha do ML indisponível para %s: %s", produto["id_produto"], exc)
    return {}


def _baixar_foto(url: str) -> Optional[bytes]:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
            return resp.content
    except requests.RequestException as exc:
        logger.warning("Foto não baixada (%s): %s", url[:80], exc)
    return None


def _card_com_foto(produto: dict[str, Any]) -> Optional[ProdutoCard]:
    foto = _baixar_foto(produto["imagem_url"]) if produto.get("imagem_url") else None
    card = ProdutoCard.de_dict(produto, foto) if foto else None
    return card if card and card.foto is not None else None


def _resumo_produto(p: dict[str, Any], nome: str) -> dict[str, Any]:
    return {k: p.get(k) for k in ("id_produto", "titulo", "preco", "preco_original", "desconto", "link", "loja")} | {
        "id": p["id_produto"], "nome": nome,
    }


# ---------------------------------------------------------------- preparo


def _candidatos(db: HistoricoDB, excluir: Iterable[str]) -> list[dict[str, Any]]:
    usados = db.produtos_usados_instagram(DIAS_SEM_REPETIR)
    bloqueados = set(usados) | set(excluir)
    return [
        c for c in db.candidatos_instagram(HORAS_CANDIDATOS)
        if c["id_produto"] not in bloqueados
        and not any(parecidos(c.get("titulo") or "", t) for t in usados.values())
    ]


def _salvar(
    db: HistoricoDB,
    slot_chave: str,
    formato: str,
    agendado_para: datetime,
    titulo: str,
    legenda: str,
    produtos: list[dict[str, Any]],
    imagens: list[Image.Image],
    usou_ia: bool,
    descricao: str,
) -> int:
    pasta = PASTA_CARDS / f"{agendado_para:%Y%m%d-%H%M}-{formato}-{uuid.uuid4().hex[:6]}"
    pasta.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(imagens):
        salvar_jpeg(img, pasta / f"{i:02d}.jpg")
    post_id = db.criar_post_instagram(
        slot_chave, agendado_para, titulo, legenda, produtos, str(pasta), len(imagens), usou_ia, formato=formato
    )
    db.registrar_disparo("Instagram", True, f"{descricao} aguardando aprovação para {agendado_para:%H:%M}")
    limpar_cards_antigos()
    return post_id


def _falhou(db: HistoricoDB, motivo: str) -> None:
    logger.warning("Post do Instagram não montado. %s", motivo)
    db.registrar_disparo("Instagram", False, motivo)


def preparar_post(
    db: HistoricoDB,
    slot_chave: str,
    agendado_para: datetime,
    excluir: Iterable[str] = (),
    agora: Optional[datetime] = None,
) -> Optional[int]:
    """Monta o post do horário e o grava como pendente. Devolve o id, ou None se não deu."""
    agora = (agora or datetime.now(_TZ)).astimezone(_TZ)
    if slot_chave == "dica":
        post_id = _preparar_dica(db, agendado_para, excluir, agora)
        if post_id:
            return post_id
        logger.info("Sem texto de dica; o horário vira 'Abaixo de R$ X'.")
        return _preparar_lista(db, slot_chave, "barato", agendado_para, excluir, agora)
    if slot_chave == "valeapena":
        post_id = _preparar_vale_a_pena(db, agendado_para, excluir, agora)
        if post_id:
            return post_id
        logger.info("Nenhum produto rendeu um 'Vale a pena?'; o horário vira 'Achado do dia'.")
        return _preparar_lista(db, slot_chave, "achado", agendado_para, excluir, agora)
    return _preparar_lista(db, slot_chave, slot_chave, agendado_para, excluir, agora)


def _preparar_lista(
    db: HistoricoDB, slot_chave: str, formato: str, agendado_para: datetime, excluir: Iterable[str], agora: datetime
) -> Optional[int]:
    lista = LISTAS[formato]
    produtos, limite = selecionar(formato, atualizar_precos(_pre_selecionar(formato, _candidatos(db, excluir))))

    cards: list[ProdutoCard] = []
    escolhidos: list[dict[str, Any]] = []
    for p in produtos:
        card = _card_com_foto(p)
        if card:
            cards.append(card)
            escolhidos.append(p)
    if len(escolhidos) < lista.minimo:
        _falhou(db, f"{FORMATOS[formato].nome}: faltaram produtos inéditos com preço conferido ({len(escolhidos)})")
        return None

    textos = gerar_textos(formato, escolhidos, agora, limite)
    for card, nome in zip(cards, textos.nomes):
        card.titulo = nome
    variante = _variante(slot_chave, agendado_para)
    imagens = montar_carrossel(
        cards, textos.titulo_capa, textos.subtitulo, variante=variante, estilo=lista.estilo_capa,
        dia=agendado_para.date(),
    )
    return _salvar(
        db, slot_chave, formato, agendado_para, textos.titulo_capa, textos.legenda,
        [_resumo_produto(p, n) for p, n in zip(escolhidos, textos.nomes)], imagens, textos.usou_ia,
        f"Carrossel '{textos.titulo_capa}' ({len(escolhidos)} produtos, paleta {PALETAS[variante % len(PALETAS)].nome})",
    )


def _preparar_vale_a_pena(
    db: HistoricoDB, agendado_para: datetime, excluir: Iterable[str], agora: datetime
) -> Optional[int]:
    candidatos = ordenar_vale_a_pena(atualizar_precos(_pre_selecionar("achado", _candidatos(db, excluir))))
    for produto in candidatos[:TENTATIVAS_VALE_A_PENA]:
        card = _card_com_foto(produto)
        if card is None:
            continue
        produto = {**produto, "detalhes": detalhes_produto(produto)}
        textos = gerar_vale_a_pena(produto, agora)
        if textos is None:
            continue
        card.titulo = textos.nome
        imagens = montar_vale_a_pena(
            card, textos.nome, textos.o_que_e, textos.pontos_fortes, textos.atencao, textos.pra_quem,
            textos.veredito, variante=_variante("valeapena", agendado_para), dia=agendado_para.date(),
            vendas=produto.get("vendas"),
        )
        return _salvar(
            db, "valeapena", "valeapena", agendado_para, f"Vale a pena? {textos.nome}", textos.legenda,
            [_resumo_produto(produto, textos.nome)], imagens, True, f"'Vale a pena?' de {textos.nome}",
        )
    return None


def produtos_da_dica(candidatos: list[dict[str, Any]], busca: list[str], maximo: int = 2) -> list[dict[str, Any]]:
    """Achadinhos do histórico cujo título contém um dos tipos de produto sugeridos para a dica."""
    termos = [_normalizar(t) for t in busca if len(t.strip()) >= 3]
    combinam = [c for c in candidatos if any(t in _normalizar(c.get("titulo") or "") for t in termos)]
    if not combinam:
        return []
    atualizados = _validos(atualizar_precos(_pre_selecionar("achado", combinam)[:8]))
    atualizados.sort(key=lambda p: (p["desconto"] or 0, p.get("avaliacao") or 0), reverse=True)
    return _sem_titulos_repetidos(atualizados)[:maximo]


def _preparar_dica(
    db: HistoricoDB, agendado_para: datetime, excluir: Iterable[str], agora: datetime
) -> Optional[int]:
    area, estilo = tema_do_dia(agendado_para.toordinal())
    textos = gerar_dica(area, estilo, db.titulos_instagram_recentes("dica"))
    if textos is None:
        return None
    cards_produtos: list[ProdutoCard] = []
    escolhidos: list[dict[str, Any]] = []
    for p in produtos_da_dica(_candidatos(db, excluir), textos.busca):
        card = _card_com_foto(p)
        if card:
            card.titulo = nome_padrao(p["titulo"])
            cards_produtos.append(card)
            escolhidos.append(p)
    nomes = [c.titulo for c in cards_produtos]
    imagens = montar_dica(
        textos.titulo_capa, textos.subtitulo, [(i.rotulo, i.titulo, i.texto, i.icone) for i in textos.itens],
        cards_produtos, variante=_variante("dica", agendado_para), dia=agendado_para.date(),
    )
    return _salvar(
        db, "dica", "dica", agendado_para, textos.titulo_capa, legenda_dica(textos, escolhidos, nomes, agora),
        [_resumo_produto(p, n) for p, n in zip(escolhidos, nomes)], imagens, True,
        f"Dica '{textos.titulo_capa}' ({area}, {len(escolhidos)} achadinhos)",
    )


def regerar_post(db: HistoricoDB, post_id: int) -> Optional[int]:
    """'Gerar outro': monta um post novo para o mesmo horário, sem os produtos do anterior."""
    antigo = db.post_instagram(post_id)
    if not antigo or antigo["status"] not in ("pendente", "aprovado", "descartado", "expirado", "falhou"):
        return None
    excluir = {p["id"] for p in antigo["produtos"]}
    agendado = datetime.fromisoformat(antigo["agendado_para"])
    novo = preparar_post(db, antigo["slot"], agendado, excluir=excluir)
    if novo:
        db.atualizar_post_instagram(post_id, "substituido")
    return novo


# ---------------------------------------------------------------- publicação


def publicar_post(db: HistoricoDB, post: dict[str, Any]) -> bool:
    """
    Publica o post aprovado pela API da Meta (`publishers/instagram_api.py`). Sem a API configurada,
    ou se a Meta recusar, devolve False: o post fica no painel (como "aprovado" ou "falhou") para ser
    publicado à mão, e `publicar_do_horario` manda o aviso no privado do Telegram.
    """
    titulo = str(post.get("titulo") or "")[:60]
    if not instagram_api.configurado():
        logger.warning("Post %s aprovado, mas a API do Instagram não está configurada; publique pelo painel.", post["id"])
        db.registrar_disparo("Instagram", False, f"'{titulo}' aprovado: publique pelo painel (API não configurada)")
        return False
    try:
        media_id = instagram_api.publicar(instagram_api.urls_publicas(post["id"], post["n_cards"]), post["legenda"] or "")
    except instagram_api.ErroInstagram as exc:
        logger.error("Falha ao publicar o post %s no Instagram: %s", post["id"], exc)
        db.atualizar_post_instagram(post["id"], "falhou", erro=str(exc))
        db.registrar_disparo("Instagram", False, f"'{titulo}' não publicou: {exc}")
        return False
    db.atualizar_post_instagram(post["id"], "publicado", ig_media_id=media_id, erro=None)
    db.registrar_disparo("Instagram", True, f"'{titulo}' publicado no Instagram")
    return True


def aviso_publicar_manual(post: dict[str, Any], agendado: datetime) -> list[tuple[str, Optional[Path]]]:
    """
    Mensagens para o privado do Telegram quando o post aprovado não pôde sair sozinho: a capa com
    o passo a passo e, numa mensagem à parte, a legenda (fácil de copiar no celular).
    """
    formato = FORMATOS.get(post.get("formato") or post.get("slot") or "")
    capa = Path(post["pasta"]) / "00.jpg" if post.get("pasta") else None
    if post.get("status") == "falhou" and post.get("erro"):
        motivo = f"A publicação automática falhou ({escape(str(post['erro'])[:300])})"
    else:
        motivo = "A publicação automática ainda não está conectada"
    instrucoes = (
        f"📲 <b>Instagram {agendado:%H:%M}: hora de postar</b>\n\n"
        f"<b>{escape(formato.nome if formato else 'Post')}</b>: {escape(str(post.get('titulo') or '')[:200])}\n"
        f"{post.get('n_cards', 0)} cards. {motivo}, então ele não saiu sozinho: "
        "no painel, toque em Compartilhar imagens → Instagram, cole a legenda abaixo e depois em "
        "\"Marquei como publicado\"."
    )
    mensagens: list[tuple[str, Optional[Path]]] = [(instrucoes, capa)]
    if post.get("legenda"):
        mensagens.append((escape(str(post["legenda"]))[:LIMITE_MENSAGEM_TELEGRAM], None))
    return mensagens


def testar_api(db: HistoricoDB) -> str:
    """
    Confere a conexão de ponta a ponta SEM publicar: baixa um card pela URL pública (Funnel) e cria
    um container na Meta, que expira sozinho em 24h sem aparecer no perfil. Devolve o relatório.
    """
    if not instagram_api.configurado():
        return "Faltam META_ACCESS_TOKEN, META_IG_USER_ID ou META_MIDIA_URL (https) no .env."
    post = next((p for p in db.posts_instagram(20) if p["status"] in ("pendente", "aprovado", "expirado", "falhou")
                 and p["n_cards"] and p["pasta"] and (Path(p["pasta"]) / "00.jpg").is_file()), None)
    if post is None:
        return "Nenhum carrossel com cards no disco para testar. Rode de novo depois que o bot montar um post."
    url = instagram_api.urls_publicas(post["id"], 1)[0]
    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        return f"A URL pública não respondeu ({exc}). Confira o Tailscale Funnel e o META_MIDIA_URL."
    if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
        return f"A URL pública respondeu HTTP {resp.status_code}. Confira o Funnel e se o bot está rodando."
    try:
        container = instagram_api.criar_container([url], "teste de conexão (não publicado)")
    except instagram_api.ErroInstagram as exc:
        return f"A imagem abre, mas a Meta recusou: {exc}"
    return f"OK: a Meta baixou o card do post {post['id']} e criou o container {container} (não publicado)."


def publicar_do_horario(
    db: HistoricoDB, slot_chave: str, agora: Optional[datetime] = None,
) -> list[tuple[str, Optional[Path]]]:
    """
    Publica o aprovado e expira o pendente. Devolve os avisos para o privado do Telegram quando o
    aprovado não saiu sozinho (API não configurada ou recusa da Meta).
    """
    agendado = agendado_hoje(slot_chave, agora)
    avisos: list[tuple[str, Optional[Path]]] = []
    for post in db.posts_instagram_do_horario(slot_chave, agendado):
        # Só o que foi aprovado no painel vai ao ar
        if post["status"] == "aprovado":
            if not publicar_post(db, post):
                avisos.extend(aviso_publicar_manual(db.post_instagram(post["id"]) or post, agendado))
        elif post["status"] == "pendente":
            db.atualizar_post_instagram(post["id"], "expirado")
            db.registrar_disparo("Instagram", False, f"'{post['titulo'][:60]}' não foi aprovado a tempo e expirou")
    return avisos


def aviso_do_horario(db: HistoricoDB, slot_chave: str, agora: Optional[datetime] = None) -> Optional[tuple[str, Optional[Path]]]:
    """
    Texto (HTML) e capa do aviso de 30 min antes, ou None se não há o que avisar (já aprovado,
    descartado ou publicado à mão).
    """
    agendado = agendado_hoje(slot_chave, agora)
    posts = db.posts_instagram_do_horario(slot_chave, agendado)
    if any(p["status"] in ("aprovado", "publicado", "descartado") for p in posts):
        return None
    pendente = next((p for p in posts if p["status"] == "pendente"), None)
    if pendente is None:
        return (
            f"⚠️ <b>Instagram {agendado:%H:%M}</b>: não consegui montar o post desse horário. "
            "O motivo está em \"Últimos disparos\" no painel.",
            None,
        )
    formato = FORMATOS.get(pendente.get("formato") or slot_chave)
    capa = Path(pendente["pasta"]) / "00.jpg" if pendente.get("pasta") else None
    texto = (
        f"⏰ <b>Instagram {agendado:%H:%M}</b>: falta 30 min e o post ainda não foi aprovado.\n\n"
        f"<b>{escape(formato.nome if formato else slot_chave)}</b>: {escape(str(pendente['titulo'] or '')[:200])}\n"
        f"{pendente['n_cards']} cards. Aprove, gere outro ou descarte no painel. Sem aprovação, ele não sai."
    )
    return texto, capa


def preparar_do_horario(db: HistoricoDB, slot_chave: str, agora: Optional[datetime] = None) -> Optional[int]:
    agendado = agendado_hoje(slot_chave, agora)
    ativos = [p for p in db.posts_instagram_do_horario(slot_chave, agendado) if p["status"] in ("pendente", "aprovado")]
    if ativos:
        logger.info("Post das %s já preparado (id %s).", f"{agendado:%H:%M}", ativos[0]["id"])
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
