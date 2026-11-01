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
    # Os abaixo têm geradores próprios (gerar_vale_a_pena, gerar_dica); os textos aqui são só o nome
    # no painel e o padrão da capa. "meme" ficou só para exibir posts antigos.
    "valeapena": Formato(
        "valeapena", "Vale a pena?", "análise de um produto", "Vale a pena?", "", "",
    ),
    "meme": Formato("meme", "Meme", "meme do nicho", "", "", ""),
    "dica": Formato("dica", "Dica", "carrossel educativo", "", "", ""),
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


# ---------------------------------------------------------------- dica (conteúdo para salvar)

# Assuntos e estilos giram por dia; o produto no fim é opcional (só se o histórico tiver algo que combine)
AREAS_DICA = ("skincare", "maquiagem", "cabelo", "unhas", "corpo e banho", "perfume")


@dataclass(frozen=True)
class EstiloDica:
    chave: str
    rotulo: str  # rótulo de cada card ("Erro 1", "Passo 2"…); vazio no mito ou verdade
    icone: str
    pauta: str


ESTILOS_DICA: tuple[EstiloDica, ...] = (
    EstiloDica("erros", "Erro", "alerta", "3 ou 4 erros comuns de {area} e como evitar cada um"),
    EstiloDica("passo", "Passo", "seta", "um passo a passo de {area}, de 3 a 5 passos, na ordem certa"),
    EstiloDica("mito", "", "check", "3 ou 4 crenças populares sobre {area}, cada uma marcada como MITO ou VERDADE"),
    EstiloDica("dicas", "Dica", "check", "3 a 5 dicas práticas e pouco óbvias de {area}"),
)

# Saúde: a página não dá conselho médico
_MEDICO = re.compile(
    r"\b(remedio|medicamento|antibiotico|corticoide|isotretinoina|roacutan|receita medica|diagnostic|"
    r"tratar acne grave|doenca|dermatite|psoriase)"
)
_PESADO = re.compile(
    r"\b(gorda|gordinha|gorducha|feia|feiosa|burra|puta|porra|caralho|merda|bosta|vadia|piranha)\b"
)
_NUMERACAO = re.compile(r"^\s*(?:(?:passo|dica|erro)\s*\d+\s*[.)\-:º°]*|\d+\s*[.)\-:º°]+)\s*", re.IGNORECASE)
AVISO_SAUDE = "Dica geral: se você tem pele sensível, alergia ou alguma condição de pele, fale com um dermatologista."


@dataclass
class ItemDica:
    titulo: str
    texto: str
    rotulo: str
    icone: str


@dataclass
class TextosDica:
    area: str
    estilo: str
    titulo_capa: str
    subtitulo: str
    itens: list[ItemDica]
    busca: list[str]
    abertura: str
    hashtags: list[str]


_SCHEMA_DICA = {
    "type": "OBJECT",
    "properties": {
        "titulo_capa": {"type": "STRING"},
        "subtitulo": {"type": "STRING"},
        "itens": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "titulo": {"type": "STRING"},
                    "texto": {"type": "STRING"},
                    "mito_ou_verdade": {"type": "STRING"},
                },
                "required": ["titulo", "texto"],
            },
        },
        "busca": {"type": "ARRAY", "items": {"type": "STRING"}},
        "abertura": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["titulo_capa", "subtitulo", "itens", "busca", "abertura", "hashtags"],
}


def _parecido(a: str, b: str) -> bool:
    pa, pb = set(_palavras(a)), set(_palavras(b))
    comuns = len(pa & pb)
    return bool(pa and pb) and comuns >= 3 and comuns >= 0.6 * min(len(pa), len(pb))


def _dica_ok(texto: Any, max_chars: int) -> bool:
    """Dica pode ter número (FPS 30, 2 dedos de protetor); o que não pode é promessa, relato ou saúde."""
    texto = str(texto or "").strip()
    if not texto or len(texto) > max_chars:
        return False
    normal = _normalizar(texto)
    barrado = (_PROIBIDOS, _EXPERIENCIA, _PROMESSAS, _MEDICO, _PESADO)
    return not any(r.search(normal) for r in barrado) and "r$" not in normal and not _sem_acento(texto)


def _prompt_dica(area: str, estilo: EstiloDica, recentes: Sequence[str]) -> str:
    evitar = "\n".join(f"- {r}" for r in list(recentes)[:20]) or "- (nenhum ainda)"
    mito = (
        '\n- Em cada item, "mito_ou_verdade": "MITO" ou "VERDADE"; o título é a crença (ex.: "Pele oleosa não '
        'precisa de hidratante") e o texto explica.' if estilo.chave == "mito" else ""
    )
    return f"""Você escreve para o Instagram {marca_padrao()}, página brasileira de achadinhos de beleza para
mulheres. Este post é um carrossel educativo para SALVAR e mandar pra amiga, sobre {area}:
{estilo.pauta.format(area=area)}.

Tom de amiga que entende do assunto, informal, direto ao ponto. Português correto, COM acentos.
Só conhecimento consolidado e seguro de beleza; nada de conselho médico, remédio ou doença de pele;
sem promessa de resultado ("garante", "100%", "cura"); sem relato pessoal ("eu uso", "testei");
sem citar marca, loja ou preço.

Não repita estes temas, já usados:
{evitar}

Responda em JSON:
- titulo_capa: gancho forte, até 34 caracteres (ex.: "3 erros que estragam sua base").
- subtitulo: até 55 caracteres.
- itens: de 3 a 5, cada um com "titulo" (até 48 caracteres) e "texto" (até 170 caracteres).{mito}
- busca: 2 a 4 tipos de produto que ajudam nesse assunto, em 1 ou 2 palavras (ex.: "protetor solar",
  "esponja", "sérum"), para procurar achadinhos.
- abertura: 1 ou 2 frases para a legenda, até 200 caracteres, com 1 ou 2 emojis.
- hashtags: 6 a 8, em português, sem acento."""


def tema_do_dia(dia_ordinal: int) -> tuple[str, EstiloDica]:
    """Assunto e estilo do dia: o assunto muda todo dia e o estilo a cada 3 dias."""
    return AREAS_DICA[dia_ordinal % len(AREAS_DICA)], ESTILOS_DICA[(dia_ordinal // 3) % len(ESTILOS_DICA)]


def gerar_dica(area: str, estilo: EstiloDica, recentes: Sequence[str], tentativas: int = 2) -> Optional[TextosDica]:
    """Textos do carrossel de dica, conferidos. None sem Gemini ou se nada passar (o horário usa outro formato)."""
    if not gemini_configurado():
        return None
    for _ in range(tentativas):
        try:
            ia = gerar_json(_prompt_dica(area, estilo, recentes), _SCHEMA_DICA, temperatura=0.8)
        except GeminiIndisponivel as exc:
            logger.warning("Dica sem Gemini (%s).", exc)
            return None
        titulo = _virgula_decimal(str(ia.get("titulo_capa") or "").strip())
        subtitulo = _virgula_decimal(str(ia.get("subtitulo") or "").strip())
        if not _dica_ok(titulo, 40) or any(_parecido(titulo, r) for r in recentes):
            logger.info("Título da dica recusado ou repetido; pedindo de novo.")
            continue
        itens: list[ItemDica] = []
        for n, item in enumerate(ia.get("itens") or [], 1):
            t, x = (_virgula_decimal(str((item or {}).get(k) or "").strip()) for k in ("titulo", "texto"))
            t = _NUMERACAO.sub("", t)  # o card já diz "Passo 1"; "1. Hidratação" repetiria
            if not (_dica_ok(t, 55) and _dica_ok(x, 190)):
                continue
            if estilo.chave == "mito":
                veredito = str(item.get("mito_ou_verdade") or "").strip().upper()
                if veredito not in ("MITO", "VERDADE"):
                    continue
                itens.append(ItemDica(t, x, veredito.capitalize(), "alerta" if veredito == "MITO" else "check"))
            else:
                itens.append(ItemDica(t, x, f"{estilo.rotulo} {len(itens) + 1}", estilo.icone))
        if len(itens) < 3:
            logger.info("Só %d itens da dica passaram nas conferências; pedindo de novo.", len(itens))
            continue
        busca = [
            " ".join(str(b).split()).lower() for b in (ia.get("busca") or [])
            if 3 <= len(str(b).strip()) <= 30
        ][:4]
        abertura = str(ia.get("abertura") or "").strip()
        if not _dica_ok(abertura, 240):
            abertura = "Salva esse pra não esquecer! 📌"
        return TextosDica(
            area=area, estilo=estilo.chave, titulo_capa=titulo,
            subtitulo=subtitulo if _dica_ok(subtitulo, 60) else "Salva pra não esquecer",
            itens=itens[:5], busca=busca, abertura=abertura, hashtags=_hashtags(ia.get("hashtags")),
        )
    return None


def legenda_dica(
    textos: TextosDica,
    produtos: Sequence[dict[str, Any]],
    nomes: Sequence[str],
    conferido_em: datetime,
) -> str:
    partes = [
        textos.abertura,
        "\n".join(f"{'✅' if i.icone != 'alerta' else '⚠️'} {i.titulo}" for i in textos.itens),
        "📌 Salva pra não esquecer e manda pra amiga que precisa!",
    ]
    if produtos:
        itens = []
        for p, nome in zip(produtos, nomes):
            linha = f"• {nome}: R$ {_format_brl(p['preco'])}"
            if p.get("desconto") and p.get("preco_original"):
                linha += f" (era R$ {_format_brl(p['preco_original'])}, {p['desconto']}% OFF)"
            itens.append(linha)
        partes += [
            "🛍️ Achadinhos que ajudam nisso:\n" + "\n".join(itens),
            "👉 Os links estão no nosso canal do WhatsApp: toca no link da bio!",
            f"Preços conferidos em {conferido_em:%d/%m} às {conferido_em:%H:%M} e podem mudar a qualquer momento.",
            AVISO_PUBLI,
        ]
    else:
        partes.append("👉 Achadinhos de beleza com desconto todo dia no nosso canal do WhatsApp: link na bio!")
    if textos.area in ("skincare", "corpo e banho"):
        partes.append(AVISO_SAUDE)
    partes.append(" ".join(textos.hashtags))
    return "\n\n".join(p for p in partes if p)[:LIMITE_LEGENDA]
