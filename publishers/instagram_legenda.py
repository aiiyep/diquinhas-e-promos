"""
Textos do carrossel do Instagram: título e subtítulo da capa, nomes curtos dos produtos e legenda.

O Gemini escreve só o que é criativo (chamada da capa, nomes curtos, abertura e hashtags). A lista
de preços da legenda é montada pelo código com os valores reais, então nenhum preço passa pela IA.
Mesmo assim, todo texto da IA é conferido: número que não seja um desconto real, o limite de preço
do formato ou a quantidade de produtos derruba o campo, e promessas que o bot não tem como saber
("frete grátis", "cupom", "últimas unidades"…) também. Campo recusado volta para o texto padrão,
então o carrossel sai mesmo sem IA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
import re
import unicodedata
from typing import Any, Optional, Sequence

from publishers.instagram_cards import marca_padrao
from publishers.gemini import GeminiIndisponivel, gemini_configurado, gerar_json
from publishers.telegram_publisher import _format_brl

logger = logging.getLogger("diquinhas_e_promos.instagram")

LIMITE_LEGENDA = 2200  # limite do Instagram
MAX_HASHTAGS = 12
HASHTAGS_PADRAO = (
    "#achadinhos", "#achadinhosdabeleza", "#beleza", "#maquiagem", "#skincare",
    "#promocao", "#ofertas", "#descontos", "#shopee", "#mercadolivre",
)
_PROIBIDOS = re.compile(
    r"frete|cupom|cupons|ultimas? unidades|ultimas? pecas|acaba hoje|so hoje|estoque|"
    r"gratis|brinde|garantid|original de fabrica|melhor preco do brasil|menor preco",
)


@dataclass(frozen=True)
class Formato:
    chave: str
    nome: str  # como aparece no painel
    pauta: str  # o que o post é, para o Gemini
    titulo_padrao: str  # aceita {n} e {limite}
    subtitulo_padrao: str  # aceita {max}
    abertura_padrao: str


FORMATOS: dict[str, Formato] = {
    "achado": Formato(
        "achado", "Achado do dia",
        "post de 'achado do dia': o primeiro produto é a estrela, os outros são bônus",
        "Achado do dia", "Até {max}% OFF hoje",
        "Olha o achado de hoje! Separamos esse e mais uns bônus com preço bom de verdade.",
    ),
    "barato": Formato(
        "barato", "Abaixo de R$ X",
        "post 'abaixo de R$ {limite}': achadinhos baratos que cabem no bolso",
        "Abaixo de R$ {limite}", "Achadinhos que cabem no bolso",
        "Achadinhos de beleza abaixo de R$ {limite} pra comprar sem culpa.",
    ),
    "top5": Formato(
        "top5", "Top do dia",
        "post 'top do dia': os maiores descontos do dia, do maior para o menor",
        "Top {n} achadinhos de hoje", "Até {max}% OFF",
        "Os maiores descontos de hoje, separados a dedo pra você.",
    ),
}


@dataclass
class TextosCarrossel:
    titulo_capa: str
    subtitulo: str
    nomes: list[str]
    legenda: str
    usou_ia: bool
    recusados: list[str] = field(default_factory=list)  # campos da IA trocados pelo padrão


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def _palavras(texto: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _normalizar(texto))


def _numeros(texto: str) -> set[int]:
    return {int(n) for n in re.findall(r"\d+", texto)}


def nome_padrao(titulo: str, max_chars: int = 42) -> str:
    """Começo do título original, cortado numa palavra inteira."""
    titulo = " ".join(titulo.split())
    if len(titulo) <= max_chars:
        return titulo
    return titulo[:max_chars].rsplit(" ", 1)[0].rstrip(" ,.-–(/") + "…"


def _acentuar_como_no_titulo(nome: str, titulo: str) -> str:
    """Troca cada palavra do nome pela grafia do título original (a IA às vezes tira os acentos)."""
    originais = {_normalizar(p): p for p in re.findall(r"[\w'-]+", titulo)}
    return re.sub(r"[\w'-]+", lambda m: originais.get(_normalizar(m.group(0)), m.group(0)), nome)


def _nome_valido(nome: str, titulo: str) -> bool:
    """Nome curto só com palavras que estão no título original (a IA não pode inventar o produto)."""
    if not nome or len(nome) > 45:
        return False
    do_titulo = set(_palavras(titulo))
    return all(p in do_titulo for p in _palavras(nome) if len(p) >= 2 or p.isdigit())


def _texto_valido(texto: str, permitidos: set[int], max_chars: int) -> bool:
    if not texto or len(texto) > max_chars:
        return False
    if _PROIBIDOS.search(_normalizar(texto)):
        return False
    return _numeros(texto) <= permitidos


def _hashtags(sugeridas: Any) -> list[str]:
    tags: list[str] = []
    for tag in sugeridas if isinstance(sugeridas, list) else []:
        limpa = "#" + re.sub(r"[^0-9a-z_]", "", _normalizar(str(tag)))
        if len(limpa) > 2 and limpa not in tags:
            tags.append(limpa)
    for tag in HASHTAGS_PADRAO:
        if len(tags) >= MAX_HASHTAGS:
            break
        if tag not in tags:
            tags.append(tag)
    return tags[:MAX_HASHTAGS]


_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "titulo_capa": {"type": "STRING"},
        "subtitulo": {"type": "STRING"},
        "nomes_curtos": {"type": "ARRAY", "items": {"type": "STRING"}},
        "abertura": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["titulo_capa", "subtitulo", "nomes_curtos", "abertura", "hashtags"],
}


def _prompt(formato: Formato, produtos: Sequence[dict[str, Any]], limite: Optional[int]) -> str:
    linhas = []
    for i, p in enumerate(produtos, 1):
        extra = f", {p['desconto']}% OFF" if p.get("desconto") else ""
        nota = f", nota {p['avaliacao']:.1f}" if p.get("avaliacao") else ""
        linhas.append(f"{i}. {p['titulo']} (R$ {_format_brl(p['preco'])}{extra}{nota})")
    pauta = formato.pauta.format(limite=limite or "")
    return f"""Você escreve para o Instagram {marca_padrao()}, página brasileira de achadinhos de beleza
com desconto, para um público feminino. Tom: amiga que achou uma promoção boa, leve e animado, sem exagero.

Este é um {pauta}. Produtos (na ordem do carrossel):
{chr(10).join(linhas)}

Responda em JSON:
- titulo_capa: chamada da capa, até 26 caracteres, impactante (ex.: "Achado do dia", "Top 5 de hoje").
- subtitulo: complemento da capa, até 50 caracteres.
- nomes_curtos: um nome curto por produto, na mesma ordem, até 40 caracteres, usando SÓ palavras
  que já estão no título do produto (pode tirar palavras, não pode trocar nem acrescentar).
- abertura: 1 ou 2 frases para abrir a legenda, até 220 caracteres, pode ter 1 ou 2 emojis.
- hashtags: 6 a 8 hashtags em português do nicho, sem acento.

Regras: escreva em português correto, COM acentos (só as hashtags vão sem acento); não cite preços em reais; só use porcentagens que estão na lista; não prometa frete grátis,
cupom, brinde, estoque acabando nem prazo ("só hoje"), porque não temos essa informação."""


def gerar_textos(
    formato_chave: str,
    produtos: Sequence[dict[str, Any]],
    conferido_em: datetime,
    limite: Optional[int] = None,
) -> TextosCarrossel:
    """
    `produtos`: dicts com `titulo`, `preco`, `preco_original` (opcional), `desconto` (int ou None)
    e `avaliacao` (opcional), na ordem do carrossel.
    """
    formato = FORMATOS[formato_chave]
    descontos = {int(p["desconto"]) for p in produtos if p.get("desconto")}
    maior = max(descontos, default=0)
    permitidos = descontos | {len(produtos)} | ({limite} if limite else set())

    titulo = formato.titulo_padrao.format(n=len(produtos), limite=limite or "")
    if "{max}" in formato.subtitulo_padrao and not maior:
        subtitulo = "Preço bom de verdade"
    else:
        subtitulo = formato.subtitulo_padrao.format(max=maior)
    abertura = formato.abertura_padrao.format(limite=limite or "")
    nomes = [nome_padrao(p["titulo"]) for p in produtos]
    tags = _hashtags([])
    usou_ia, recusados = False, []

    if gemini_configurado():
        try:
            ia = gerar_json(_prompt(formato, produtos, limite), _SCHEMA)
            usou_ia = True
            if _texto_valido(str(ia.get("titulo_capa", "")).strip(), permitidos, 30):
                titulo = str(ia["titulo_capa"]).strip()
            else:
                recusados.append("titulo_capa")
            if _texto_valido(str(ia.get("subtitulo", "")).strip(), permitidos, 60):
                subtitulo = str(ia["subtitulo"]).strip()
            else:
                recusados.append("subtitulo")
            if _texto_valido(str(ia.get("abertura", "")).strip(), permitidos, 300):
                abertura = str(ia["abertura"]).strip()
            else:
                recusados.append("abertura")
            sugeridos = ia.get("nomes_curtos") if isinstance(ia.get("nomes_curtos"), list) else []
            for i, p in enumerate(produtos):
                nome = " ".join(str(sugeridos[i]).split()) if i < len(sugeridos) else ""
                if _nome_valido(nome, p["titulo"]):
                    nomes[i] = _acentuar_como_no_titulo(nome, p["titulo"])
                else:
                    recusados.append(f"nome {i + 1}")
            tags = _hashtags(ia.get("hashtags"))
        except GeminiIndisponivel as exc:
            logger.warning("Legenda do Instagram sem IA (%s); usando o texto padrão.", exc)
        if recusados:
            logger.info("Campos da IA trocados pelo padrão: %s", ", ".join(recusados))

    return TextosCarrossel(
        titulo_capa=titulo,
        subtitulo=subtitulo,
        nomes=nomes,
        legenda=montar_legenda(abertura, produtos, nomes, tags, conferido_em),
        usou_ia=usou_ia,
        recusados=recusados,
    )


def montar_legenda(
    abertura: str,
    produtos: Sequence[dict[str, Any]],
    nomes: Sequence[str],
    hashtags: Sequence[str],
    conferido_em: datetime,
) -> str:
    itens = []
    for i, (p, nome) in enumerate(zip(produtos, nomes), 1):
        linha = f"{i}. {nome}: R$ {_format_brl(p['preco'])}"
        if p.get("desconto") and p.get("preco_original"):
            linha += f" (era R$ {_format_brl(p['preco_original'])}, {p['desconto']}% OFF)"
        itens.append(linha)
    partes = [
        abertura,
        "\n".join(itens),
        "👉 O link de cada um está no nosso canal do WhatsApp: toca no link da bio!",
        f"Preços conferidos em {conferido_em:%d/%m} às {conferido_em:%H:%M} e podem mudar a qualquer momento.",
        " ".join(hashtags),
    ]
    legenda = "\n\n".join(p for p in partes if p)
    return legenda[:LIMITE_LEGENDA]
