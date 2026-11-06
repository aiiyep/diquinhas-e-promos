"""
Reels semiautomáticos: o bot monta um vídeo de slides (sem música) e o usuário posta pelo app do
Instagram, onde escolhe uma música em alta. A API da Meta não acessa a biblioteca de músicas.

Formatos:
- **rotina**: um produto por passo de uma rotina ("Como ser a amiga que SEMPRE tá cheirosa":
  banho → pós-banho → axila → íntimo → cabelo → perfume).
- **sazonal**: Black Friday (do dia 1º de novembro até a Cyber Monday) e amigo secreto (até 24/12),
  um produto por categoria. Fora dessas datas o horário vira rotina.
- **duelo**: "Isso ou aquilo?", dois produtos do mesmo tipo por slide; o público comenta 1 ou 2.

Os produtos vêm do que o Telegram postou nos últimos dias, com o preço reconsultado na loja (quem não
pode ser conferido fica de fora, como nos carrosséis). Os slides não mostram preço nem desconto, porque
o Reels continua sendo visto por meses; a chamada final leva para o canal do WhatsApp (link na bio).

Agenda: `REELS_DIAS` (padrão "seg,qua,sex,dom") às `REELS_HORA` (padrão 10:00). O bot prepara o vídeo,
deixa no painel e avisa no privado do Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
import logging
from pathlib import Path
import shutil
import threading
from typing import Any, Iterable, Optional
import uuid
from zoneinfo import ZoneInfo

from config import env
from database.historico_ofertas import HistoricoDB
from orquestrador.instagram import (
    _baixar_foto,
    _normalizar,
    _validos,
    atualizar_precos,
    parecidos,
)
from publishers.instagram_cards import PALETAS, _abrir_foto, salvar_jpeg
from publishers.instagram_legenda import AVISO_PUBLI, AVISO_SAUDE, nome_padrao
from publishers.reels_slides import (
    ItemReel,
    Slide,
    duracao,
    montar_video,
    slide_duelo,
    slide_final,
    slide_gancho,
    slide_produto,
)

logger = logging.getLogger("diquinhas_e_promos.reels")

_TZ = ZoneInfo("America/Sao_Paulo")
PASTA_REELS = Path(__file__).resolve().parent.parent / "data" / "reels"
HORAS_CANDIDATOS = 24 * 10
DIAS_SEM_REPETIR = 21
DIAS_GUARDAR = 10
POR_PASSO = 3  # candidatos por passo com o preço reconsultado

SEG_GANCHO, SEG_PRODUTO, SEG_DUELO, SEG_FINAL = 2.2, 2.4, 3.4, 3.0

DIAS_SEMANA = {"seg": "mon", "ter": "tue", "qua": "wed", "qui": "thu", "sex": "fri", "sab": "sat", "dom": "sun"}
_NOMES_DIAS = list(DIAS_SEMANA)  # seg=0 … dom=6, igual a date.weekday()

_gerando = threading.Lock()  # um vídeo por vez (o droplet tem 1 vCPU)


@dataclass(frozen=True)
class Passo:
    rotulo: str
    termos: tuple[str, ...]
    evitar: tuple[str, ...] = ()

    def combina(self, titulo: str) -> bool:
        t = _normalizar(titulo)
        return any(termo in t for termo in self.termos) and not any(e in t for e in self.evitar)


@dataclass(frozen=True)
class Tema:
    chave: str
    formato: str  # rotina | sazonal | duelo
    rotulo: str  # pílula do primeiro slide
    ganchos: tuple[str, ...]
    passos: tuple[Passo, ...]
    hashtags: tuple[str, ...]
    minimo: int = 3
    maximo: int = 6
    prefixo_passo: bool = True  # "PASSO 1 · BANHO"
    chamada: str = "Os links de tudo estão no nosso canal do WhatsApp: toca no link da bio!"
    fecho: str = "📌 Salva pra montar a sua rotina."
    aviso_saude: bool = False


# ---------------------------------------------------------------- categorias e temas

_MAKE = Passo("Maquiagem", ("batom", "gloss", "lip tint", "base liquida", "base matte", "bb cream", "corretivo",
                            "mascara de cilios", "rimel", "paleta de sombra", "blush", "po compacto", "delineador"))
_SKIN = Passo("Skincare", ("serum", "hidratante facial", "protetor solar", "sabonete facial", "gel de limpeza",
                           "vitamina c", "niacinamida", "acido hialuronico", "agua micelar"), ("capilar",))
_CABELO = Passo("Cabelo", ("shampoo", "condicionador", "mascara capilar", "leave-in", "leave in", "oleo capilar",
                           "finalizador", "protetor termico", "creme de pentear"))
_PERFUME = Passo("Perfume", ("perfume", "body splash", "colonia", "eau de parfum", "eau de toilette"), ("capilar",))
_CORPO = Passo("Corpo", ("hidratante corporal", "locao hidratante", "oleo corporal", "esfoliante corporal",
                         "manteiga corporal", "creme hidratante", "body lotion"), ("facial", "maos"))

# O Instagram aceita até 5 hashtags por post/Reels (desde dez/2025): 3 do tema + #achadinhos + #publi
_TAGS = ("#achadinhos",)

TEMAS: tuple[Tema, ...] = (
    Tema(
        "cheirosa", "rotina", "Rotina de cheirosa",
        ("Como ser a amiga que SEMPRE tá cheirosa", "Não seja a amiga fedida"),
        (
            Passo("Banho", ("sabonete liquido", "sabonete em barra", "gel de banho", "oleo de banho",
                            "sabonete corporal", "esfoliante"), ("intimo", "facial")),
            Passo("Pós-banho", ("hidratante corporal", "locao hidratante", "manteiga corporal", "oleo corporal",
                                "creme hidratante", "body lotion"), ("facial", "maos")),
            Passo("Axila", ("desodorante", "antitranspirante")),
            Passo("Íntimo", ("sabonete intimo", "higiene intima", "sabonete liquido intimo")),
            Passo("Cabelo", ("perfume capilar", "body splash capilar", "leave-in", "leave in", "shampoo")),
            Passo("Perfume", ("perfume", "body splash", "colonia", "eau de parfum", "eau de toilette"), ("capilar",)),
        ),
        ("#cheirosa", "#rotinadebeleza", "#perfume") + _TAGS,
    ),
    Tema(
        "cabelo", "rotina", "Cabelo sem frizz",
        ("Cabelo sem frizz no calor: a rotina completa", "Seu cabelo vive com frizz? Salva essa rotina"),
        (
            Passo("Lavar", ("shampoo",)),
            Passo("Condicionar", ("condicionador",)),
            Passo("Tratar", ("mascara capilar", "mascara de hidratacao", "mascara de nutricao",
                             "mascara de reconstrucao", "ampola")),
            Passo("Finalizar", ("leave-in", "leave in", "creme de pentear", "finalizador")),
            Passo("Proteger", ("protetor termico",)),
            Passo("Brilho", ("oleo capilar", "serum capilar", "oleo reparador")),
        ),
        ("#cabelo", "#rotinacapilar", "#cabelosemfrizz") + _TAGS,
    ),
    Tema(
        "skincare", "rotina", "Skincare simples",
        ("Skincare de quem não tem tempo: só 4 passos", "Rotina de skincare pra começar hoje"),
        (
            Passo("Limpar", ("sabonete facial", "gel de limpeza", "espuma de limpeza", "agua micelar"), ("corporal",)),
            Passo("Tratar", ("serum", "vitamina c", "niacinamida", "acido hialuronico"), ("capilar",)),
            Passo("Hidratar", ("hidratante facial", "creme facial", "gel creme")),
            Passo("Proteger", ("protetor solar",)),
        ),
        ("#skincare", "#skincarebrasil", "#rotinadeskincare") + _TAGS,
        aviso_saude=True,
    ),
    Tema(
        "make", "rotina", "Make de 5 minutos",
        ("Make de 5 minutos pra quando você tá atrasada", "A make básica que salva qualquer dia"),
        (
            Passo("Pele", ("base liquida", "base matte", "bb cream", "cc cream", "base facial", "skin tint")),
            Passo("Corretivo", ("corretivo",)),
            Passo("Bochechas", ("blush", "bronzer", "iluminador")),
            Passo("Olhos", ("mascara de cilios", "rimel", "delineador", "lapis de olho")),
            Passo("Boca", ("batom", "gloss", "lip tint", "lip oil", "balm labial")),
            Passo("Fixar", ("fixador de maquiagem", "po compacto", "po solto", "bruma fixadora")),
        ),
        ("#maquiagem", "#makedodia", "#make") + _TAGS,
    ),
    Tema(
        "unhas", "rotina", "Unhas em casa",
        ("Unha de salão sem sair de casa", "Kit pra fazer as unhas em casa"),
        (
            Passo("Preparar", ("removedor de esmalte", "oleo de cuticula", "amolecedor de cuticula", "lixa de unha")),
            Passo("Base", ("base fortalecedora", "base para unhas", "base incolor", "base de unha")),
            Passo("Cor", ("esmalte",), ("removedor", "base", "top coat")),
            Passo("Finalizar", ("top coat", "extra brilho", "secante")),
        ),
        ("#unhas", "#unhasdecoradas", "#esmalte") + _TAGS,
    ),
    Tema(
        "blackfriday", "sazonal", "Black Friday",
        ("Lista de Black Friday: coloca no carrinho JÁ", "Black Friday tá chegando: monta sua lista de beleza",
         "O que colocar no carrinho pra Black Friday"),
        (_MAKE, _SKIN, _CABELO, _PERFUME, _CORPO),
        ("#blackfriday", "#blackfridaybeleza", "#promocao") + _TAGS,
        maximo=5, prefixo_passo=False,
        chamada="As ofertas de Black Friday saem todo dia no nosso canal do WhatsApp: toca no link da bio!",
        fecho="📌 Salva pra não esquecer na hora de comprar.",
    ),
    Tema(
        "amigosecreto", "sazonal", "Amigo secreto",
        ("Presentes de beleza pro amigo secreto", "Não sabe o que dar no amigo secreto? Salva esse"),
        (
            Passo("Pra quem ama make", _MAKE.termos, _MAKE.evitar),
            Passo("Pra quem ama skincare", _SKIN.termos, _SKIN.evitar),
            Passo("Pra quem ama perfume", _PERFUME.termos, _PERFUME.evitar),
            Passo("Pra quem ama cabelo", _CABELO.termos, _CABELO.evitar),
            Passo("Pra quem ama se cuidar", _CORPO.termos, _CORPO.evitar),
        ),
        ("#amigosecreto", "#presentedenatal", "#natal") + _TAGS,
        maximo=5, prefixo_passo=False,
        fecho="📌 Salva pra lembrar na hora do sorteio.",
    ),
    Tema(
        "duelo", "duelo", "Isso ou aquilo?",
        ("Isso ou aquilo? Comenta 1 ou 2", "Escolhe um de cada e comenta sua combinação",
         "Só pode escolher um: 1 ou 2?"),
        (
            Passo("Batom", ("batom", "lip tint", "gloss")),
            Passo("Perfume", ("perfume", "eau de parfum", "eau de toilette", "colonia"), ("capilar",)),
            Passo("Body splash", ("body splash",), ("capilar",)),
            Passo("Máscara de cílios", ("mascara de cilios", "rimel")),
            Passo("Protetor solar", ("protetor solar",)),
            Passo("Sérum", ("serum", "vitamina c", "niacinamida"), ("capilar",)),
            Passo("Hidratante", ("hidratante corporal", "locao hidratante", "body lotion"), ("facial",)),
            Passo("Shampoo", ("shampoo",)),
            Passo("Máscara capilar", ("mascara capilar", "mascara de hidratacao", "mascara de nutricao")),
            Passo("Base", ("base liquida", "base matte", "bb cream", "skin tint")),
            Passo("Paleta de sombras", ("paleta de sombra",)),
            Passo("Esmalte", ("esmalte",), ("removedor",)),
            Passo("Blush", ("blush",)),
            Passo("Óleo capilar", ("oleo capilar", "oleo reparador")),
        ),
        ("#issoouaquilo", "#qualvoceescolhe", "#beleza") + _TAGS,
        minimo=2, maximo=3,
        chamada="Os links estão no nosso canal do WhatsApp: toca no link da bio!",
        fecho="Comenta sua combinação (ex.: 1, 2, 1) 👇",
    ),
)
TEMAS_POR_CHAVE = {t.chave: t for t in TEMAS}
NOMES_FORMATOS = {"rotina": "Rotina", "sazonal": "Data especial", "duelo": "Isso ou aquilo"}


# ---------------------------------------------------------------- agenda e escolha do tema


def reels_ativo() -> bool:
    return env("REELS_ATIVO", "1") != "0"


def dias_configurados() -> list[str]:
    """Dias da semana dos Reels ("seg", "qua"…), lendo `REELS_DIAS`."""
    dias = [d.strip().lower()[:3] for d in env("REELS_DIAS", "seg,qua,sex,dom").split(",")]
    validos = [d for d in dias if d in DIAS_SEMANA]
    if not validos:
        logger.warning("REELS_DIAS inválido; usando seg,qua,sex,dom.")
        return ["seg", "qua", "sex", "dom"]
    return sorted(set(validos), key=_NOMES_DIAS.index)


def horario() -> tuple[int, int]:
    texto = env("REELS_HORA", "10:00")
    try:
        hora, minuto = (int(x) for x in texto.split(":"))
        assert 0 <= hora < 24 and 0 <= minuto < 60
        return hora, minuto
    except (ValueError, AssertionError):
        logger.warning("REELS_HORA inválido (%r); usando 10:00.", texto)
        return 10, 0


def black_friday(ano: int) -> date:
    """Sexta depois da 4ª quinta-feira de novembro."""
    primeiro = date(ano, 11, 1)
    quinta = primeiro + timedelta(days=(3 - primeiro.weekday()) % 7)
    return quinta + timedelta(days=22)


def tema_sazonal(dia: date) -> Optional[Tema]:
    bf = black_friday(dia.year)
    if date(dia.year, 11, 1) <= dia <= bf + timedelta(days=3):  # até a Cyber Monday
        return TEMAS_POR_CHAVE["blackfriday"]
    if bf + timedelta(days=4) <= dia <= date(dia.year, 12, 24):
        return TEMAS_POR_CHAVE["amigosecreto"]
    return None


def formato_do_dia(dia: date, recentes: int = 0) -> str:
    """
    Na época de Black Friday/Natal: sazonal, rotina, duelo, sazonal (pela ordem dos dias da semana).
    No resto do ano: rotina e duelo alternados. Fora dos dias configurados, gira pelo nº de Reels.
    """
    plano = ["sazonal", "rotina", "duelo", "sazonal"] if tema_sazonal(dia) else ["rotina", "duelo"]
    dias = dias_configurados()
    nome = _NOMES_DIAS[dia.weekday()]
    indice = dias.index(nome) if nome in dias else recentes
    return plano[indice % len(plano)]


def temas_em_ordem(dia: date, recentes: list[str]) -> list[Tema]:
    """O formato do dia primeiro (tema menos usado na frente), depois os outros como reserva."""
    preferido = formato_do_dia(dia, len(recentes))

    def uso(t: Tema) -> int:
        # tema que nunca saiu (ou saiu há mais tempo) vem antes
        return recentes.index(t.chave) if t.chave in recentes else 10_000

    sazonal = tema_sazonal(dia)
    disponiveis = [t for t in TEMAS if t.formato != "sazonal" or t is sazonal]
    ordem = ("sazonal", "rotina", "duelo")
    return sorted(
        disponiveis,
        key=lambda t: (t.formato != preferido, ordem.index(t.formato), -uso(t)),
    )


def _gancho(tema: Tema, recentes: list[str]) -> str:
    return tema.ganchos[recentes.count(tema.chave) % len(tema.ganchos)]


# ---------------------------------------------------------------- produtos


def _candidatos(db: HistoricoDB, excluir: Iterable[str]) -> list[dict[str, Any]]:
    usados = db.produtos_usados_reels(DIAS_SEM_REPETIR)
    bloqueados = set(usados) | set(excluir)
    return [
        c for c in _validos(db.candidatos_instagram(HORAS_CANDIDATOS))
        if c["id_produto"] not in bloqueados and not any(parecidos(c.get("titulo") or "", t) for t in usados.values())
    ]


def _melhores(candidatos: list[dict[str, Any]], passo: Passo, n: int) -> list[dict[str, Any]]:
    combinam = [c for c in candidatos if passo.combina(c.get("titulo") or "")]
    combinam.sort(key=lambda c: (c.get("desconto") or 0, c.get("avaliacao") or 0), reverse=True)
    return combinam[:n]


def _item(produto: dict[str, Any]) -> Optional[ItemReel]:
    foto = _abrir_foto(_baixar_foto(produto["imagem_url"])) if produto.get("imagem_url") else None
    # o slide tem 2 linhas de ~28 caracteres para o nome
    return ItemReel(nome_padrao(produto["titulo"] or "", 54), foto) if foto is not None else None


def _conferidos(grupos: list[list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Reconsulta os preços de uma vez só; o que sumiu da loja (ou não pôde ser conferido) sai."""
    unicos = {c["id_produto"]: c for grupo in grupos for c in grupo}
    return {c["id_produto"]: c for c in _validos(atualizar_precos(list(unicos.values())))}


def _escolher_rotina(tema: Tema, candidatos: list[dict[str, Any]]) -> list[tuple[Passo, dict[str, Any], ItemReel]]:
    grupos = [_melhores(candidatos, passo, POR_PASSO) for passo in tema.passos]
    conferidos = _conferidos(grupos)
    escolhidos: list[tuple[Passo, dict[str, Any], ItemReel]] = []
    for passo, grupo in zip(tema.passos, grupos):
        for c in grupo:
            p = conferidos.get(c["id_produto"])
            if not p or any(p["id_produto"] == e[1]["id_produto"] or parecidos(p["titulo"] or "", e[1]["titulo"] or "")
                            for e in escolhidos):
                continue
            item = _item(p)
            if item:
                escolhidos.append((passo, p, item))
                break
        if len(escolhidos) >= tema.maximo:
            break
    return escolhidos


def _escolher_duelos(tema: Tema, candidatos: list[dict[str, Any]], inicio: int) -> list[tuple[Passo, list[tuple[dict, ItemReel]]]]:
    # gira o começo da lista de tipos para os duelos não repetirem sempre os mesmos
    tipos = list(tema.passos[inicio % len(tema.passos):] + tema.passos[: inicio % len(tema.passos)])
    com_par = [(t, g) for t in tipos if len(g := _melhores(candidatos, t, 4)) >= 2][: tema.maximo + 2]
    conferidos = _conferidos([g for _, g in com_par])
    usados: set[str] = set()
    rodadas: list[tuple[Passo, list[tuple[dict, ItemReel]]]] = []
    for tipo, grupo in com_par:
        par: list[tuple[dict, ItemReel]] = []
        for c in grupo:
            p = conferidos.get(c["id_produto"])
            if not p or p["id_produto"] in usados or any(parecidos(p["titulo"] or "", o["titulo"] or "") for o, _ in par):
                continue
            item = _item(p)
            if item:
                par.append((p, item))
            if len(par) == 2:
                break
        if len(par) == 2:
            usados.update(p["id_produto"] for p, _ in par)
            rodadas.append((tipo, par))
        if len(rodadas) >= tema.maximo:
            break
    return rodadas


def _resumo(p: dict[str, Any], nome: str, rotulo: str) -> dict[str, Any]:
    return {"id": p["id_produto"], "titulo": p.get("titulo"), "nome": nome, "passo": rotulo, "link": p.get("link"),
            "loja": p.get("loja")}


# ---------------------------------------------------------------- legenda


def _legenda(tema: Tema, gancho: str, linhas: list[str]) -> str:
    partes = [
        f"{gancho} ✨" if tema.formato != "duelo" else f"{gancho} 👀",
        "\n".join(linhas),
        tema.fecho,
        f"💬 {tema.chamada}",
        AVISO_SAUDE if tema.aviso_saude else "",
        AVISO_PUBLI,
        " ".join(tema.hashtags),
    ]
    return "\n\n".join(p for p in partes if p)


# ---------------------------------------------------------------- preparo


def _montar(tema: Tema, gancho: str, candidatos: list[dict[str, Any]], variante: int) -> Optional[tuple[list[Slide], list[dict], list[str]]]:
    paleta = PALETAS[variante % len(PALETAS)]
    if tema.formato == "duelo":
        rodadas = _escolher_duelos(tema, candidatos, variante)
        if len(rodadas) < tema.minimo:
            return None
        n = len(rodadas)
        slides = [Slide(slide_gancho(paleta, gancho, tema.rotulo, subtitulo=f"{n} rodadas"), SEG_GANCHO)]
        produtos, linhas = [], []
        for i, (tipo, ((pa, a), (pb, b))) in enumerate(rodadas, 1):
            slides.append(Slide(slide_duelo(paleta, f"Rodada {i} · {tipo.rotulo}", a, b, f"{i}/{n}"), SEG_DUELO))
            produtos += [_resumo(pa, a.nome, tipo.rotulo), _resumo(pb, b.nome, tipo.rotulo)]
            linhas.append(f"Rodada {i} · {tipo.rotulo}\n1) {a.nome}\n2) {b.nome}")
        slides.append(Slide(slide_final(paleta, "Comenta sua combinação!",
                                        ["Ex.: " + ", ".join("1" if k % 2 == 0 else "2" for k in range(n)),
                                         "Os links estão no canal do WhatsApp", "Toque no link da bio"]), SEG_FINAL))
        return slides, produtos, linhas

    escolhidos = _escolher_rotina(tema, candidatos)
    if len(escolhidos) < tema.minimo:
        return None
    n = len(escolhidos)
    subtitulo = f"{n} passos" if tema.formato == "rotina" else f"{n} achadinhos de beleza"
    slides = [Slide(slide_gancho(paleta, gancho, tema.rotulo, subtitulo=subtitulo), SEG_GANCHO)]
    produtos, linhas = [], []
    for i, (passo, p, item) in enumerate(escolhidos, 1):
        rotulo = f"Passo {i} · {passo.rotulo}" if tema.prefixo_passo else passo.rotulo
        slides.append(Slide(slide_produto(paleta, rotulo, item, f"{i}/{n}"), SEG_PRODUTO))
        produtos.append(_resumo(p, item.nome, passo.rotulo))
        linhas.append(f"{i}. {passo.rotulo}: {item.nome}")
    slides.append(Slide(slide_final(paleta, "Quer os links?",
                                    ["Toque no link da bio", "Siga o canal no WhatsApp", "Achadinhos novos todo dia"]),
                        SEG_FINAL))
    return slides, produtos, linhas


def preparar_reel(db: HistoricoDB, dia: Optional[date] = None, excluir: Iterable[str] = ()) -> Optional[int]:
    """Monta o Reels do dia (vídeo + legenda) e grava como pendente. None se nenhum tema tiver produtos."""
    if not _gerando.acquire(blocking=False):
        logger.info("Já tem um Reels sendo montado; pedido ignorado.")
        return None
    try:
        dia = dia or datetime.now(_TZ).date()
        recentes = db.temas_reels_recentes()
        candidatos = _candidatos(db, excluir)
        variante = dia.toordinal() + len(recentes)
        for tema in temas_em_ordem(dia, recentes):
            gancho = _gancho(tema, recentes)
            montado = _montar(tema, gancho, candidatos, variante)
            if montado is None:
                logger.info("Reels '%s' sem produtos suficientes; tentando outro tema.", tema.chave)
                continue
            slides, produtos, linhas = montado
            pasta = PASTA_REELS / f"{dia:%Y%m%d}-{tema.chave}-{uuid.uuid4().hex[:6]}"
            pasta.mkdir(parents=True, exist_ok=True)
            salvar_jpeg(slides[0].imagem, pasta / "capa.jpg")
            montar_video(slides, pasta / "video.mp4")
            reel_id = db.criar_reel(
                dia.isoformat(), tema.formato, tema.chave, gancho, _legenda(tema, gancho, linhas), produtos,
                str(pasta), duracao(slides),
            )
            db.registrar_disparo("Reels", True, f"Reels '{gancho}' pronto no painel ({len(slides)} slides)")
            limpar_antigos()
            return reel_id
        db.registrar_disparo("Reels", False, "Reels não montado: faltam produtos com foto para os temas")
        logger.warning("Nenhum tema de Reels teve produtos suficientes.")
        return None
    finally:
        _gerando.release()


def gerando() -> bool:
    return _gerando.locked()


def regerar(db: HistoricoDB, reel_id: int) -> Optional[int]:
    """'Gerar outro': novo Reels para o mesmo dia, sem os produtos do anterior."""
    antigo = db.reel(reel_id)
    if not antigo or antigo["status"] not in ("pendente", "descartado"):
        return None
    novo = preparar_reel(db, date.fromisoformat(antigo["dia"]), excluir={p["id"] for p in antigo["produtos"]})
    if novo:
        db.atualizar_reel(reel_id, "substituido")
    return novo


def preparar_do_dia(db: HistoricoDB, agora: Optional[datetime] = None) -> tuple[Optional[int], str, Optional[Path]]:
    """Job do agendador: monta o Reels do dia (se ainda não houver) e devolve (id, aviso HTML, capa)."""
    dia = (agora or datetime.now(_TZ)).astimezone(_TZ).date()
    existentes = [r for r in db.reels_do_dia(dia.isoformat()) if r["status"] in ("pendente", "postado")]
    reel_id = existentes[0]["id"] if existentes else preparar_reel(db, dia)
    if reel_id is None:
        return None, (
            "⚠️ <b>Reels de hoje</b>: não consegui montar o vídeo (faltam produtos com foto para os temas). "
            "Tente \"Gerar Reels agora\" no painel mais tarde."
        ), None
    reel = db.reel(reel_id)
    capa = Path(reel["pasta"]) / "capa.jpg" if reel and reel.get("pasta") else None
    return reel_id, aviso_pronto(reel), capa


def aviso_pronto(reel: dict[str, Any]) -> str:
    return (
        f"🎬 <b>Reels de hoje pronto</b>: {escape(str(reel['titulo'] or ''))}\n"
        f"{len(reel['produtos'])} produtos · {reel['segundos']:.0f}s · sem música\n\n"
        "No painel: <b>Compartilhar vídeo</b> → Instagram (Reels), escolha uma música em alta, cole a legenda "
        "(<b>Copiar legenda</b>) e poste. Melhor horário: entre 18h e 21h. Depois toque em "
        "\"Marquei como postado\"."
    )


def limpar_antigos(dias: int = DIAS_GUARDAR) -> None:
    if not PASTA_REELS.exists():
        return
    corte = datetime.now(_TZ).date() - timedelta(days=dias)
    for pasta in PASTA_REELS.iterdir():
        try:
            if pasta.is_dir() and datetime.strptime(pasta.name[:8], "%Y%m%d").date() < corte:
                shutil.rmtree(pasta, ignore_errors=True)
        except ValueError:
            continue
