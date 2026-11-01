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
    # Os dois abaixo têm geradores próprios (gerar_vale_a_pena, gerar_meme); os textos aqui são só
    # o nome no painel e o padrão da capa
    "valeapena": Formato(
        "valeapena", "Vale a pena?", "análise de um produto", "Vale a pena?", "", "",
    ),
    "meme": Formato("meme", "Meme", "meme do nicho", "", "", ""),
}

# Aviso de publicidade (CONAR) em todo post com produto e link de afiliado
AVISO_PUBLI = "Contém links de afiliado. #publi"


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


# Palavras que só existem com acento: se aparecem sem, a IA escreveu sem acentos
_SEM_ACENTO = re.compile(
    r"\b(nao|voce|voces|mes|tambem|ja|so|ate|entao|tao|porem|alem|ninguem|alguem|tres|ultimo|unico|pe|po)\b"
)


def _sem_acento(texto: str) -> bool:
    return bool(_SEM_ACENTO.search(texto.lower()))


def _virgula_decimal(texto: str) -> str:
    """4.9 → 4,9 (a IA às vezes escreve número no formato americano)."""
    return re.sub(r"(\d)\.(\d)", r"\1,\2", texto)


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
    if _PROIBIDOS.search(_normalizar(texto)) or _sem_acento(texto):
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
        AVISO_PUBLI,
        " ".join(hashtags),
    ]
    legenda = "\n\n".join(p for p in partes if p)
    return legenda[:LIMITE_LEGENDA]


# ---------------------------------------------------------------- "Vale a pena?"

# A página nunca usou o produto: nada de relato pessoal ("testei", "na minha pele"…)
_EXPERIENCIA = re.compile(
    r"\b(testei|testamos|usei|usamos|uso todo|eu uso|amei|amamos|comprei|compramos|"
    r"minha pele|meu cabelo|meus cabelos|meu rosto|na minha|no meu|minha experiencia|"
    r"recomendo|indico)\b"
)
# Promessas de saúde/resultado que nenhuma ficha garante
_PROMESSAS = re.compile(r"\b(cura|curar|garante|garantido|garantida|milagre|milagroso|definitiv|100 ?%)")


@dataclass
class TextosValeAPena:
    nome: str
    o_que_e: str
    pontos_fortes: list[str]
    atencao: list[str]
    pra_quem: list[str]
    veredito: str
    legenda: str


_SCHEMA_VALE = {
    "type": "OBJECT",
    "properties": {
        "informacao_suficiente": {"type": "BOOLEAN"},
        "nome_curto": {"type": "STRING"},
        "o_que_e": {"type": "STRING"},
        "pontos_fortes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "atencao": {"type": "ARRAY", "items": {"type": "STRING"}},
        "pra_quem": {"type": "ARRAY", "items": {"type": "STRING"}},
        "veredito": {"type": "STRING"},
        "abertura": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": [
        "informacao_suficiente", "nome_curto", "o_que_e", "pontos_fortes", "atencao", "pra_quem", "veredito",
        "abertura", "hashtags",
    ],
}


def _numeros_da_ficha(produto: dict[str, Any]) -> set[int]:
    """Números que o texto pode citar: os que estão no título, na ficha, no desconto, na nota e nas vendas."""
    detalhes = produto.get("detalhes") or {}
    textos = [produto.get("titulo") or "", detalhes.get("descricao") or "", *(detalhes.get("atributos") or [])]
    permitidos = set().union(*(_numeros(t) for t in textos))
    if produto.get("desconto"):
        permitidos.add(int(produto["desconto"]))
    if produto.get("avaliacao"):
        permitidos |= _numeros(f"{produto['avaliacao']:.1f}") | {5}
    if produto.get("vendas"):
        vendas = int(produto["vendas"])
        permitidos |= {vendas, vendas // 1000, vendas // 100 * 100}
    return permitidos


def _frase_ok(texto: Any, permitidos: set[int], max_chars: int) -> bool:
    texto = str(texto or "").strip()
    if not texto or len(texto) > max_chars:
        return False
    normal = _normalizar(texto)
    if _PROIBIDOS.search(normal) or _EXPERIENCIA.search(normal) or _PROMESSAS.search(normal) or "r$" in normal:
        return False
    return not _sem_acento(texto) and _numeros(texto) <= permitidos


def _lista_ok(itens: Any, permitidos: set[int], max_chars: int, minimo: int, maximo: int) -> Optional[list[str]]:
    itens = [_virgula_decimal(str(i).strip()) for i in (itens if isinstance(itens, list) else [])]
    validos = [i for i in itens if _frase_ok(i, permitidos, max_chars)]
    return validos[:maximo] if len(validos) >= minimo else None


def _prompt_vale(produto: dict[str, Any]) -> str:
    detalhes = produto.get("detalhes") or {}
    dados = [f"Título na loja: {produto['titulo']}"]
    if produto.get("loja_nome"):
        dados.append(f"Loja: {produto['loja_nome']}")
    if produto.get("avaliacao"):
        dados.append(f"Nota das compradoras: {produto['avaliacao']:.1f} de 5")
    if produto.get("vendas"):
        dados.append(f"Vendidos: {produto['vendas']}")
    if produto.get("desconto"):
        dados.append(f"Desconto agora: {produto['desconto']}%")
    if detalhes.get("descricao"):
        dados.append(f"Descrição oficial: {detalhes['descricao']}")
    if detalhes.get("atributos"):
        dados.append("Ficha técnica: " + "; ".join(detalhes["atributos"]))
    return f"""Você escreve para o Instagram {marca_padrao()}, página brasileira de achadinhos de beleza
para um público feminino. Post "Vale a pena?": análise honesta de UM produto, em linguagem informal
de amiga (pode usar "miga", "pra", "tá"), sem exagero. Português correto, COM acentos (não, você,
mês, já). Números decimais com vírgula (4,9).

Dados do produto (é tudo o que se sabe):
{chr(10).join(dados)}

A página NUNCA usou o produto. Proibido: relato pessoal ("testei", "usei", "amei", "na minha pele",
"recomendo"), inventar resultado, ingrediente, tamanho ou característica que não esteja nos dados,
promessas ("garante", "cura", "100%"), preço em reais, frete, cupom, estoque, prazo. Pode usar
conhecimento geral sobre o TIPO de produto (ex.: "corretivo no tom errado marca a olheira") e, se
for uma marca/linha muito conhecida, o que é amplamente sabido sobre ela, sem números. Escreva com acentos.

Responda em JSON:
- informacao_suficiente: false se os dados forem pobres demais para uma análise útil (aí o resto pode ir vazio).
- nome_curto: até 40 caracteres, só com palavras do título.
- o_que_e: 1 frase, até 150 caracteres, explicando o que o produto é.
- pontos_fortes: 3 itens de até 85 caracteres. No máximo 1 deles pode ser prova social (nota, vendas ou
  desconto); os outros falam do produto: para que serve, textura, ativos, praticidade, rendimento…
- atencao: 1 ou 2 itens de até 95 caracteres: o que conferir antes de comprar (tom, tipo de pele, tamanho…).
- pra_quem: 2 ou 3 itens de até 65 caracteres, começando por verbo ou "quem" (ex.: "Quer disfarçar olheira").
- veredito: até 140 caracteres, começando com "Vale a pena se" ou "Vale a pena pra", falando do uso
  do produto (não só de nota ou desconto).
- abertura: 1 ou 2 frases para a legenda, até 220 caracteres, com 1 ou 2 emojis.
- hashtags: 6 a 8, em português, sem acento."""


def gerar_vale_a_pena(produto: dict[str, Any], conferido_em: datetime, tentativas: int = 2) -> Optional[TextosValeAPena]:
    """
    Textos do "Vale a pena?" de um produto. None quando não há Gemini, quando ele diz que os dados
    não bastam ou quando o texto não passa nas conferências (aí o horário usa outro produto ou formato).
    """
    if not gemini_configurado():
        return None
    permitidos = _numeros_da_ficha(produto)
    for _ in range(tentativas):
        try:
            ia = gerar_json(_prompt_vale(produto), _SCHEMA_VALE, temperatura=0.7)
            for campo in ("o_que_e", "veredito", "abertura"):
                ia[campo] = _virgula_decimal(str(ia.get(campo) or "").strip())
        except GeminiIndisponivel as exc:
            logger.warning("'Vale a pena?' sem Gemini (%s).", exc)
            return None
        if not ia.get("informacao_suficiente"):
            logger.info("Gemini achou poucos dados para o 'Vale a pena?' de %s.", produto["titulo"][:60])
            return None
        nome = " ".join(str(ia.get("nome_curto") or "").split())
        nome = _acentuar_como_no_titulo(nome, produto["titulo"]) if _nome_valido(nome, produto["titulo"]) \
            else nome_padrao(produto["titulo"], 40)
        pontos = _lista_ok(ia.get("pontos_fortes"), permitidos, 90, 2, 3)
        atencao = _lista_ok(ia.get("atencao"), permitidos, 100, 1, 2)
        pra_quem = _lista_ok(ia.get("pra_quem"), permitidos, 70, 1, 3)
        campos_ok = (
            pontos and atencao and pra_quem
            and _frase_ok(ia.get("o_que_e"), permitidos, 160)
            and _frase_ok(ia.get("veredito"), permitidos, 150)
        )
        if not campos_ok:
            logger.info("Texto do 'Vale a pena?' não passou nas conferências; pedindo de novo.")
            continue
        abertura = str(ia.get("abertura") or "").strip()
        if not _frase_ok(abertura, permitidos, 260):
            abertura = f"Vale a pena? Separamos o que importa sobre {nome} antes de você comprar 👀"
        return TextosValeAPena(
            nome=nome,
            o_que_e=str(ia["o_que_e"]).strip(),
            pontos_fortes=pontos,
            atencao=atencao,
            pra_quem=pra_quem,
            veredito=str(ia["veredito"]).strip(),
            legenda=_legenda_vale(abertura, produto, pontos, atencao, _hashtags(ia.get("hashtags")), conferido_em),
        )
    return None


def _legenda_vale(
    abertura: str, produto: dict[str, Any], pontos: list[str], atencao: list[str], tags: list[str],
    conferido_em: datetime,
) -> str:
    preco = f"💰 R$ {_format_brl(produto['preco'])}"
    if produto.get("desconto") and produto.get("preco_original"):
        preco += f" (era R$ {_format_brl(produto['preco_original'])}, {produto['desconto']}% OFF)"
    prova = []
    if produto.get("avaliacao"):
        prova.append(f"nota {produto['avaliacao']:.1f}".replace(".", ","))
    if produto.get("vendas") and produto["vendas"] >= 50:
        from publishers.instagram_cards import formatar_vendas

        prova.append(formatar_vendas(produto["vendas"]))
    partes = [
        abertura,
        "\n".join(f"✅ {p}" for p in pontos),
        "\n".join(f"⚠️ {a}" for a in atencao),
        preco + (f"\n⭐ {' • '.join(prova)}" if prova else ""),
        "👉 O link está no nosso canal do WhatsApp: toca no link da bio!",
        f"Preço conferido em {conferido_em:%d/%m} às {conferido_em:%H:%M} e pode mudar a qualquer momento.",
        AVISO_PUBLI,
        " ".join(tags),
    ]
    return "\n\n".join(p for p in partes if p)[:LIMITE_LEGENDA]


# ---------------------------------------------------------------- meme

_PESADO = re.compile(
    r"\b(gorda|gordinha|gorducha|feia|feiosa|burra|puta|porra|caralho|merda|bosta|vadia|piranha|"
    r"bolsonaro|lula|deus|igreja|macumba)\b"
)
HASHTAGS_MEME = ["#memes", "#humor", "#achadinhos", "#beleza", "#skincare", "#maquiagem", "#comprinhas", "#shopee"]

# Usados quando o Gemini está fora do ar ou nada do que ele sugeriu passou
MEMES_PADRAO = (
    "eu: esse mês não compro mais nada de skincare\n\neu 3 minutos depois: “só vou dar uma olhadinha no carrinho”",
    "ninguém:\n\neu às 2 da manhã comparando 14 corretivos iguais",
    "minha conta bancária vendo eu abrir a Shopee pela quinta vez no dia",
    "“é só um batom” — eu, pela décima vez esse mês",
    "eu não sou viciada em skincare\n\neu só tenho 9 séruns abertos ao mesmo tempo",
    "a encomenda chegando e eu agindo como se fosse surpresa",
    "eu: vou usar até acabar\n\nmeu banheiro: 6 shampoos pela metade",
    "quando a amiga pergunta “onde você comprou?” e você já manda o link antes dela terminar a frase",
    "eu salvando 47 posts de maquiagem pra nunca mais olhar",
    "rotina de skincare: 10 passos\n\nminha paciência às 23h: 1 passo",
    "POV: você entrou pra comprar um demaquilante e saiu com 7 itens no carrinho",
    "eu fingindo que preciso de mais uma paleta de sombras",
)


@dataclass
class TextosMeme:
    texto: str
    legenda: str
    usou_ia: bool


_SCHEMA_MEME = {
    "type": "OBJECT",
    "properties": {
        "memes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"texto": {"type": "STRING"}, "legenda": {"type": "STRING"}},
                "required": ["texto", "legenda"],
            },
        },
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["memes", "hashtags"],
}


def _parecido(a: str, b: str) -> bool:
    pa, pb = set(_palavras(a)), set(_palavras(b))
    comuns = len(pa & pb)
    return bool(pa and pb) and comuns >= 3 and comuns >= 0.6 * min(len(pa), len(pb))


def _meme_ok(texto: str, recentes: Sequence[str]) -> bool:
    texto = texto.strip()
    # terminar em "minha conta bancária:" pede uma imagem que o card não tem
    if not texto or len(texto) > 220 or texto.count("\n") > 6 or texto.endswith(":"):
        return False
    normal = _normalizar(texto)
    if _PESADO.search(normal) or _PROIBIDOS.search(normal) or "r$" in normal or _sem_acento(texto):
        return False
    return not any(_parecido(texto, r) for r in recentes)


def _prompt_meme(recentes: Sequence[str]) -> str:
    evitar = "\n".join(f"- {r.replace(chr(10), ' / ')}" for r in list(recentes)[:15]) or "- (nenhum ainda)"
    return f"""Você cria memes para o Instagram {marca_padrao()}, página brasileira de achadinhos de beleza
para mulheres. O meme vai num card com cara de print de tweet, só texto, sem imagem.

Crie 3 memes curtos e diferentes entre si sobre a vida de quem ama beleza, skincare, maquiagem,
cabelo e compras online (carrinho cheio, encomenda chegando, "só vou olhar", amiga pedindo o link,
rotina de skincare, gaveta de maquiagem…). Formatos que funcionam: "eu: … / eu 3 minutos depois: …",
"ninguém: / eu: …", "POV: …", frase única de identificação. A ideia é a pessoa marcar a amiga.

Regras: português do Brasil, informal, COM acentos (não, você, mês, já, só, até); até 200 caracteres; use \\n para quebrar linha;
sem emoji no texto do meme; a graça tem que estar TODA no texto (o card não tem imagem, então
nunca termine com dois-pontos esperando uma foto); sem palavrão; sem zoar corpo, peso ou aparência de ninguém; sem política
ou religião; sem citar marca, loja ou preço.

Não repita nem parafraseie estes, que já foram usados:
{evitar}

Para cada meme, "legenda": 1 frase curta para a legenda do post (até 120 caracteres, pode ter emoji).
Também 6 a 8 hashtags em português, sem acento."""


def gerar_meme(recentes: Sequence[str]) -> TextosMeme:
    """Meme do dia: o primeiro do Gemini que passar nas conferências; senão, um da lista padrão."""
    texto, frase, tags, usou_ia = "", "", list(HASHTAGS_MEME), False
    if gemini_configurado():
        try:
            ia = gerar_json(_prompt_meme(recentes), _SCHEMA_MEME, temperatura=1.0)
            for meme in ia.get("memes") or []:
                candidato = str((meme or {}).get("texto") or "").replace("\\n", "\n").strip()
                if _meme_ok(candidato, recentes):
                    texto, usou_ia = candidato, True
                    legenda = str(meme.get("legenda") or "").strip()
                    if legenda and len(legenda) <= 140 and not _PESADO.search(_normalizar(legenda)) \
                            and not _PROIBIDOS.search(_normalizar(legenda)):
                        frase = legenda
                    tags = _hashtags(ia.get("hashtags"))[:8] or tags
                    break
            if not texto:
                logger.info("Nenhum meme do Gemini passou nas conferências; usando um da lista padrão.")
        except GeminiIndisponivel as exc:
            logger.warning("Meme sem Gemini (%s); usando um da lista padrão.", exc)
    if not texto:
        livres = [m for m in MEMES_PADRAO if not any(_parecido(m, r) or m == r for r in recentes)]
        texto = (livres or list(MEMES_PADRAO))[0]
    partes = [
        frase or "Quem nunca? 😂",
        "Marca aquela amiga que é assim 👇",
        "E as ofertas de verdade ficam no nosso canal do WhatsApp: link na bio!",
        " ".join(tags),
    ]
    return TextosMeme(texto=texto, legenda="\n\n".join(partes), usou_ia=usou_ia)
