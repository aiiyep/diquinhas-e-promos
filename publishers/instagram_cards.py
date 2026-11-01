"""
Cards do carrossel do Instagram (1080×1350, retrato), desenhados com Pillow.

Os layouts são fixos no código e variam por paleta e por estilo de capa, então o bot monta
as artes sozinho, sem IA e sem Canva. Foto, preço e desconto são sempre os reais do produto:
nada é inventado (sem "De" informado, o card mostra só o preço).

Carrossel = capa + um card por produto + card final (chamada para o canal do WhatsApp, "link na bio").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import BytesIO
import math
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from config import env
from publishers.telegram_publisher import _format_brl, _to_float

LARGURA, ALTURA = 1080, 1350
_FONTES = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_MARGEM = 72


@dataclass(frozen=True)
class Paleta:
    nome: str
    fundo1: str
    fundo2: str
    destaque: str  # preço, selo de desconto, detalhes
    texto: str
    suave: str  # textos secundários e "De" riscado
    selo_texto: str = "#FFFFFF"
    cartao: str = "#FFFFFF"


PALETAS: tuple[Paleta, ...] = (
    Paleta("rosa", "#FFF0F5", "#FBC8DA", "#D6336C", "#3B1426", "#9C6B7E"),
    Paleta("lilas", "#F5F0FF", "#D9C8F7", "#7048C8", "#24173F", "#7C6D99"),
    Paleta("pessego", "#FFF6EE", "#FFD3BA", "#E8590C", "#3D1E0C", "#A0715A"),
    Paleta("menta", "#EEFBF6", "#BDEBD9", "#0C8F6A", "#0E2E24", "#5E8C7D"),
    Paleta("noite", "#2A1530", "#120A17", "#FF5FA2", "#FFF4F8", "#C7A3B6", cartao="#FFFFFF"),
)

ESTILOS_CAPA = ("grade", "destaque", "lista")


def marca_padrao() -> str:
    """@ da página (INSTAGRAM_ARROBA no .env), ou o nome do projeto se não estiver definido."""
    arroba = (env("INSTAGRAM_ARROBA") or "").strip().lstrip("@")
    return f"@{arroba}" if arroba else "Diquinhas e Promos"


# ---------------------------------------------------------------- utilitários de desenho


def _fonte(peso: str, tamanho: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONTES / f"Poppins-{peso}.ttf"), tamanho)


def _rgb(cor: str) -> tuple[int, int, int]:
    cor = cor.lstrip("#")
    return tuple(int(cor[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _fundo(paleta: Paleta) -> Image.Image:
    """Degradê diagonal suave entre as duas cores da paleta."""
    vertical = Image.linear_gradient("L").resize((LARGURA, ALTURA))
    horizontal = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90).resize((LARGURA, ALTURA))
    mascara = Image.blend(vertical, ImageOps.mirror(horizontal), 0.35)
    return Image.composite(
        Image.new("RGB", (LARGURA, ALTURA), _rgb(paleta.fundo2)),
        Image.new("RGB", (LARGURA, ALTURA), _rgb(paleta.fundo1)),
        mascara,
    )


def _sombra(base: Image.Image, caixa: tuple[int, int, int, int], raio: int, forca: int = 60) -> None:
    camada = Image.new("RGBA", base.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = caixa
    ImageDraw.Draw(camada).rounded_rectangle((x0, y0 + 14, x1, y1 + 14), raio, fill=(0, 0, 0, forca))
    base.alpha_composite(camada.filter(ImageFilter.GaussianBlur(22)))


def _colar_arredondado(base: Image.Image, img: Image.Image, pos: tuple[int, int], raio: int) -> None:
    mascara = Image.new("L", img.size, 0)
    ImageDraw.Draw(mascara).rounded_rectangle((0, 0, img.width - 1, img.height - 1), raio, fill=255)
    base.paste(img, pos, mascara)


def _foto_no_cartao(foto: Optional[Image.Image], tamanho: tuple[int, int], fundo: str) -> Image.Image:
    """Foto inteira (sem cortar o produto) centralizada num cartão da cor `fundo`."""
    cartao = Image.new("RGB", tamanho, _rgb(fundo))
    if foto is None:
        return cartao
    folga = int(min(tamanho) * 0.06)
    encaixada = ImageOps.contain(foto.convert("RGB"), (tamanho[0] - 2 * folga, tamanho[1] - 2 * folga), Image.LANCZOS)
    cartao.paste(encaixada, ((tamanho[0] - encaixada.width) // 2, (tamanho[1] - encaixada.height) // 2))
    return cartao


def _quebrar(texto: str, fonte: ImageFont.FreeTypeFont, largura: int, max_linhas: int) -> list[str]:
    """Quebra o texto em até `max_linhas` linhas que cabem em `largura`, com reticências no fim."""
    palavras, linhas, atual = _juntar_moeda(texto.split()), [], ""
    for i, palavra in enumerate(palavras):
        teste = f"{atual} {palavra}".strip()
        if fonte.getlength(teste) <= largura:
            atual = teste
            continue
        if atual:
            linhas.append(atual)
        atual = palavra
        if len(linhas) == max_linhas:
            atual = ""
            linhas[-1] = _reticencias(" ".join([linhas[-1], *palavras[i:]]), fonte, largura)
            break
    if atual:
        linhas.append(atual)
    if len(linhas) > max_linhas:
        linhas = linhas[: max_linhas - 1] + [_reticencias(" ".join(linhas[max_linhas - 1 :]), fonte, largura)]
    return linhas


def _juntar_moeda(palavras: list[str]) -> list[str]:
    """Mantém "R$" grudado no valor, para "R$ 30" não quebrar entre linhas."""
    saida: list[str] = []
    for palavra in palavras:
        if saida and saida[-1].upper() == "R$":
            saida[-1] += " " + palavra
        else:
            saida.append(palavra)
    return saida


def _reticencias(texto: str, fonte: ImageFont.FreeTypeFont, largura: int) -> str:
    if fonte.getlength(texto) <= largura:
        return texto
    while texto and fonte.getlength(texto + "…") > largura:
        texto = texto[:-1]
    return texto.rstrip(" ,.-–(") + "…"


def _texto_centro(draw: ImageDraw.ImageDraw, y: int, texto: str, fonte, cor: str, x0: int = 0, x1: int = LARGURA) -> int:
    largura = fonte.getlength(texto)
    draw.text((x0 + (x1 - x0 - largura) / 2, y), texto, font=fonte, fill=cor)
    return y + _altura(fonte)


def _altura(fonte: ImageFont.FreeTypeFont) -> int:
    subida, descida = fonte.getmetrics()
    return subida + descida


def _estrela(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, cor: str) -> None:
    pontos = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        raio = r if i % 2 == 0 else r * 0.45
        pontos.append((cx + raio * math.cos(ang), cy + raio * math.sin(ang)))
    draw.polygon(pontos, fill=cor)


def _seta(draw: ImageDraw.ImageDraw, x: float, y: float, cor: str, tamanho: int = 22) -> None:
    """Seta "→" desenhada (a fonte não tem o caractere)."""
    draw.line((x, y, x + tamanho * 1.6, y), fill=cor, width=5)
    draw.line((x + tamanho * 0.9, y - tamanho * 0.6, x + tamanho * 1.6, y), fill=cor, width=5)
    draw.line((x + tamanho * 0.9, y + tamanho * 0.6, x + tamanho * 1.6, y), fill=cor, width=5)


def _selo_desconto(base: Image.Image, cx: int, cy: int, raio: int, pct: int, paleta: Paleta) -> None:
    draw = ImageDraw.Draw(base)
    draw.ellipse((cx - raio, cy - raio, cx + raio, cy + raio), fill=paleta.destaque)
    num = _fonte("Black", int(raio * 0.62))
    off = _fonte("Bold", int(raio * 0.30))
    texto = f"-{pct}%"
    h_total = _altura(num) * 0.78 + _altura(off) * 0.8
    y = cy - h_total / 2 - raio * 0.04
    draw.text((cx - num.getlength(texto) / 2, y), texto, font=num, fill=paleta.selo_texto)
    draw.text((cx - off.getlength("OFF") / 2, y + _altura(num) * 0.78), "OFF", font=off, fill=paleta.selo_texto)


def _cabecalho(base: Image.Image, paleta: Paleta, marca: str, direita: str = "") -> None:
    draw = ImageDraw.Draw(base)
    fonte = _fonte("SemiBold", 30)
    draw.ellipse((_MARGEM, 62, _MARGEM + 18, 80), fill=paleta.destaque)
    # @ do Instagram fica como está (minúsculo); nome solto vai em caixa alta
    draw.text((_MARGEM + 32, 52), marca if marca.startswith("@") else marca.upper(), font=fonte, fill=paleta.texto)
    if direita:
        draw.text((LARGURA - _MARGEM - fonte.getlength(direita), 52), direita, font=fonte, fill=paleta.suave)


def _rodape_link(base: Image.Image, paleta: Paleta, texto: str = "Link no canal  •  link na bio") -> None:
    draw = ImageDraw.Draw(base)
    fonte = _fonte("SemiBold", 32)
    largura = fonte.getlength(texto) + 80
    x0, y0 = (LARGURA - largura) / 2, ALTURA - 130
    draw.rounded_rectangle((x0, y0, x0 + largura, y0 + 72), 36, fill=paleta.texto)
    draw.text((x0 + 40, y0 + 13), texto, font=fonte, fill=paleta.fundo1)


# ---------------------------------------------------------------- dados do produto


@dataclass
class ProdutoCard:
    titulo: str
    preco: float
    preco_original: Optional[float] = None
    avaliacao: Optional[float] = None
    foto: Optional[Image.Image] = None

    @property
    def desconto(self) -> Optional[int]:
        if self.preco_original and self.preco_original > self.preco:
            pct = round((1 - self.preco / self.preco_original) * 100)
            return pct or None
        return None

    @classmethod
    def de_dict(cls, dados: dict[str, Any], foto: Optional[Image.Image | bytes | str | Path] = None) -> "ProdutoCard":
        """Aceita os dicts do bot (`preco`/`preco_promocional`, `preco_original`, `avaliacao`)."""
        preco = _to_float(dados.get("preco") if dados.get("preco") is not None else dados.get("preco_promocional"))
        if not preco:
            raise ValueError("produto sem preço")
        return cls(
            titulo=" ".join(str(dados.get("titulo") or "").split()),
            preco=preco,
            preco_original=_to_float(dados.get("preco_original")),
            avaliacao=_to_float(dados.get("avaliacao")),
            foto=_abrir_foto(foto),
        )


def _abrir_foto(foto: Any) -> Optional[Image.Image]:
    if foto is None or isinstance(foto, Image.Image):
        return foto
    try:
        img = Image.open(BytesIO(foto) if isinstance(foto, bytes) else foto)
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def _bloco_preco(draw: ImageDraw.ImageDraw, p: ProdutoCard, paleta: Paleta, x: int, y: int, tam: int = 92) -> int:
    """Desenha "De R$ X" (riscado, se houver) e "R$ Y"; devolve o y final."""
    if p.desconto:
        fonte_de = _fonte("Medium", int(tam * 0.42))
        texto_de = f"De R$ {_format_brl(p.preco_original)}"
        draw.text((x, y), texto_de, font=fonte_de, fill=paleta.suave)
        meio = y + _altura(fonte_de) * 0.52
        inicio = x + fonte_de.getlength("De ")
        draw.line((inicio, meio, x + fonte_de.getlength(texto_de), meio), fill=paleta.suave, width=4)
        y += int(_altura(fonte_de) * 0.95)
    fonte_rs = _fonte("Bold", int(tam * 0.45))
    fonte_valor = _fonte("ExtraBold", tam)
    draw.text((x, y + tam * 0.40), "R$", font=fonte_rs, fill=paleta.destaque)
    draw.text((x + fonte_rs.getlength("R$ ") + 4, y), _format_brl(p.preco), font=fonte_valor, fill=paleta.destaque)
    return y + int(_altura(fonte_valor) * 0.9)


def _nota(draw: ImageDraw.ImageDraw, p: ProdutoCard, paleta: Paleta, x: float, y: float) -> None:
    if not p.avaliacao:
        return
    fonte = _fonte("SemiBold", 34)
    _estrela(draw, x + 18, y + 24, 20, "#F5A524")
    draw.text((x + 48, y), f"{p.avaliacao:.1f}".replace(".", ","), font=fonte, fill=paleta.texto)


# ---------------------------------------------------------------- cards


def card_produto(p: ProdutoCard, paleta: Paleta, marca: str, posicao: str = "") -> Image.Image:
    base = _fundo(paleta).convert("RGBA")
    _cabecalho(base, paleta, marca, posicao)

    lado = LARGURA - 2 * _MARGEM
    alto = 690
    caixa = (_MARGEM, 140, _MARGEM + lado, 140 + alto)
    _sombra(base, caixa, 48)
    _colar_arredondado(base, _foto_no_cartao(p.foto, (lado, alto), paleta.cartao), caixa[:2], 48)
    if p.desconto:
        _selo_desconto(base, LARGURA - _MARGEM - 70, 140 + 60, 112, p.desconto, paleta)

    draw = ImageDraw.Draw(base)
    y = caixa[3] + 36
    fonte_titulo = _fonte("SemiBold", 40)
    for linha in _quebrar(p.titulo, fonte_titulo, lado, 2):
        draw.text((_MARGEM, y), linha, font=fonte_titulo, fill=paleta.texto)
        y += int(_altura(fonte_titulo) * 0.98)
    y += 14
    fim = _bloco_preco(draw, p, paleta, _MARGEM, y)
    _nota(draw, p, paleta, LARGURA - _MARGEM - 130, fim - 70)
    _rodape_link(base, paleta)
    return base.convert("RGB")


def _titulo_capa(draw, linhas: Sequence[str], paleta: Paleta, y: int, tam: int = 104) -> int:
    fonte = _fonte("Black", tam)
    for linha in linhas:
        y = _texto_centro(draw, y, linha.upper(), fonte, paleta.texto) - int(tam * 0.18)
    return y


def _quebrar_titulo_capa(titulo: str, tam: int) -> list[str]:
    return _quebrar(titulo.upper(), _fonte("Black", tam), LARGURA - 2 * _MARGEM, 3)


def _arraste(base: Image.Image, paleta: Paleta) -> None:
    draw = ImageDraw.Draw(base)
    fonte = _fonte("SemiBold", 34)
    texto = "Arrasta pro lado"
    largura = fonte.getlength(texto) + 60
    x = (LARGURA - largura) / 2
    y = ALTURA - 118
    draw.text((x, y), texto, font=fonte, fill=paleta.texto)
    _seta(draw, x + fonte.getlength(texto) + 18, y + 26, paleta.destaque)


def capa(
    produtos: Sequence[ProdutoCard],
    paleta: Paleta,
    marca: str,
    titulo: str,
    subtitulo: str = "",
    estilo: str = "grade",
    dia: Optional[date] = None,
) -> Image.Image:
    base = _fundo(paleta).convert("RGBA")
    # O servidor roda em UTC: quem chama passa o dia de São Paulo
    _cabecalho(base, paleta, marca, (dia or date.today()).strftime("%d/%m"))
    draw = ImageDraw.Draw(base)

    if estilo == "destaque" and produtos:
        _capa_destaque(base, draw, produtos[0], paleta, titulo, subtitulo)
    elif estilo == "lista":
        _capa_lista(base, draw, produtos, paleta, titulo, subtitulo)
    else:
        _capa_grade(base, draw, produtos, paleta, titulo, subtitulo)
    _arraste(base, paleta)
    return base.convert("RGB")


def _subtitulo(draw, texto: str, paleta: Paleta, y: int) -> int:
    if not texto:
        return y
    fonte = _fonte("Medium", 38)
    for linha in _quebrar(texto, fonte, LARGURA - 2 * _MARGEM, 2):
        y = _texto_centro(draw, y, linha, fonte, paleta.suave)
    return y


def _capa_grade(base, draw, produtos, paleta, titulo, subtitulo) -> None:
    tam = 88
    y = _titulo_capa(draw, _quebrar_titulo_capa(titulo, tam), paleta, 150, tam)
    y = _subtitulo(draw, subtitulo, paleta, y + 24) + 36
    itens = list(produtos[:4])
    gap = 28
    lado = (LARGURA - 2 * _MARGEM - gap) // 2
    disponivel = ALTURA - 160 - y
    lado = min(lado, (disponivel - gap) // 2) if len(itens) > 2 else min(lado, disponivel)
    total_l = lado * 2 + gap
    x0 = (LARGURA - total_l) // 2
    for i, p in enumerate(itens):
        x = x0 + (i % 2) * (lado + gap)
        yy = y + (i // 2) * (lado + gap)
        _sombra(base, (x, yy, x + lado, yy + lado), 36, 45)
        _colar_arredondado(base, _foto_no_cartao(p.foto, (lado, lado), paleta.cartao), (x, yy), 36)
        if p.desconto:
            _etiqueta(base, x + 18, yy + 18, f"-{p.desconto}%", paleta)


def _etiqueta(base, x: int, y: int, texto: str, paleta: Paleta, tam: int = 36) -> None:
    draw = ImageDraw.Draw(base)
    fonte = _fonte("ExtraBold", tam)
    largura = fonte.getlength(texto) + tam
    draw.rounded_rectangle((x, y, x + largura, y + tam * 1.55), tam * 0.78, fill=paleta.destaque)
    draw.text((x + tam / 2, y + tam * 0.16), texto, font=fonte, fill=paleta.selo_texto)


def _capa_destaque(base, draw, p: ProdutoCard, paleta, titulo, subtitulo) -> None:
    tam = 96
    y = _titulo_capa(draw, _quebrar_titulo_capa(titulo, tam), paleta, 150, tam)
    y = _subtitulo(draw, subtitulo, paleta, y + 20) + 70
    lado = min(LARGURA - 2 * _MARGEM - 120, ALTURA - 190 - y)
    x = (LARGURA - lado) // 2
    _sombra(base, (x, y, x + lado, y + lado), 56)
    _colar_arredondado(base, _foto_no_cartao(p.foto, (lado, lado), paleta.cartao), (x, y), 56)
    if p.desconto:
        _selo_desconto(base, x + lado - 20, y + 30, 110, p.desconto, paleta)


def _capa_lista(base, draw, produtos, paleta, titulo, subtitulo) -> None:
    tam = 92
    y = _titulo_capa(draw, _quebrar_titulo_capa(titulo, tam), paleta, 150, tam)
    y = _subtitulo(draw, subtitulo, paleta, y + 16) + 30
    itens = list(produtos[:4])
    if not itens:
        return
    gap = 22
    alto = min(190, (ALTURA - 170 - y - gap * (len(itens) - 1)) // len(itens))
    fonte_t = _fonte("SemiBold", 31)
    fonte_p = _fonte("ExtraBold", 44)
    coluna_preco = max(fonte_p.getlength(f"R$ {_format_brl(p.preco)}") for p in itens) + 44
    for p in itens:
        caixa = (_MARGEM, y, LARGURA - _MARGEM, y + alto)
        _sombra(base, caixa, 32, 35)
        ImageDraw.Draw(base).rounded_rectangle(caixa, 32, fill=paleta.cartao)
        foto = _foto_no_cartao(p.foto, (alto - 24, alto - 24), paleta.cartao)
        _colar_arredondado(base, foto, (_MARGEM + 12, y + 12), 24)
        tx = _MARGEM + alto + 16
        largura_txt = int(LARGURA - _MARGEM - tx - coluna_preco - 12)
        d = ImageDraw.Draw(base)
        linhas = _quebrar(p.titulo, fonte_t, largura_txt, 2)
        ty = y + (alto - len(linhas) * _altura(fonte_t) * 0.95) / 2
        for linha in linhas:
            d.text((tx, ty), linha, font=fonte_t, fill="#2B2B2B")
            ty += _altura(fonte_t) * 0.95
        preco = f"R$ {_format_brl(p.preco)}"
        px = LARGURA - _MARGEM - 28 - fonte_p.getlength(preco)
        d.text((px, y + alto / 2 - (_altura(fonte_p) * (0.95 if p.desconto else 0.55))), preco, font=fonte_p, fill=paleta.destaque)
        if p.desconto:
            etiqueta = f"-{p.desconto}%"
            fe = _fonte("ExtraBold", 30)
            ex = LARGURA - _MARGEM - 28 - fe.getlength(etiqueta) - 30
            _etiqueta(base, int(ex), int(y + alto / 2 + 8), etiqueta, paleta, 30)
        y += alto + gap


def card_final(paleta: Paleta, marca: str, chamada: str = "Quer o link?") -> Image.Image:
    base = _fundo(paleta).convert("RGBA")
    _cabecalho(base, paleta, marca)
    draw = ImageDraw.Draw(base)
    tam = 128
    y = 300
    for linha in _quebrar(chamada.upper(), _fonte("Black", tam), LARGURA - 2 * _MARGEM, 2):
        y = _texto_centro(draw, y, linha, _fonte("Black", tam), paleta.texto) - 20
    y += 70
    passos = (
        ("1", "Toque no link da bio"),
        ("2", "Siga o canal no WhatsApp"),
        ("3", "Promoções novas todo dia"),
    )
    fonte_num = _fonte("ExtraBold", 46)
    fonte_txt = _fonte("SemiBold", 46)
    for num, texto in passos:
        draw.ellipse((_MARGEM + 40, y, _MARGEM + 124, y + 84), fill=paleta.destaque)
        draw.text((_MARGEM + 82 - fonte_num.getlength(num) / 2, y + 10), num, font=fonte_num, fill=paleta.selo_texto)
        draw.text((_MARGEM + 160, y + 10), texto, font=fonte_txt, fill=paleta.texto)
        y += 130
    y += 40
    _texto_centro(draw, y, "Salva esse post pra não perder", _fonte("Medium", 38), paleta.suave)
    _rodape_link(base, paleta, "Link na bio")
    return base.convert("RGB")


# ---------------------------------------------------------------- "Vale a pena?" e meme

# A Poppins não tem emoji nem a maioria dos símbolos: o que não for texto latino sai do card
_PERMITIDOS_EXTRA = set("…–—“”‘’•·ºª°")


def limpar_para_card(texto: str) -> str:
    limpo = "".join(c for c in texto if ord(c) < 0x2000 or c in _PERMITIDOS_EXTRA)
    return "\n".join(" ".join(linha.split()) for linha in limpo.splitlines()).strip()


def formatar_vendas(vendas: Optional[int]) -> str:
    if not vendas or vendas < 50:
        return ""
    if vendas >= 1000:
        return f"+{vendas // 1000} mil vendidos"
    return f"+{vendas // 50 * 50} vendidos"


def _icone(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, tipo: str, paleta: Paleta) -> None:
    """Bolinha com ✓, ! ou → desenhado à mão (a fonte não tem os símbolos)."""
    cor = paleta.destaque if tipo != "alerta" else "#E8890C"
    draw.ellipse((x - r, y - r, x + r, y + r), fill=cor)
    branco = paleta.selo_texto
    espessura = max(4, int(r * 0.22))
    if tipo == "check":
        draw.line((x - r * 0.45, y + r * 0.02, x - r * 0.1, y + r * 0.38, x + r * 0.5, y - r * 0.35),
                  fill=branco, width=espessura, joint="curve")
    elif tipo == "alerta":
        draw.line((x, y - r * 0.5, x, y + r * 0.12), fill=branco, width=espessura)
        draw.ellipse((x - r * 0.13, y + r * 0.3, x + r * 0.13, y + r * 0.56), fill=branco)
    else:
        draw.line((x - r * 0.45, y, x + r * 0.4, y), fill=branco, width=espessura)
        draw.line((x + r * 0.05, y - r * 0.38, x + r * 0.45, y, x + r * 0.05, y + r * 0.38),
                  fill=branco, width=espessura, joint="curve")


def _rotulo(draw: ImageDraw.ImageDraw, x: int, y: int, texto: str, paleta: Paleta) -> int:
    fonte = _fonte("Bold", 30)
    largura = fonte.getlength(texto.upper()) + 48
    draw.rounded_rectangle((x, y, x + largura, y + 56), 28, fill=paleta.destaque)
    draw.text((x + 24, y + 9), texto.upper(), font=fonte, fill=paleta.selo_texto)
    return y + 56


def card_topicos(
    paleta: Paleta,
    marca: str,
    rotulo: str,
    titulo: str,
    itens: Sequence[str],
    icone: str = "check",
    foto: Optional[Image.Image] = None,
    posicao: str = "",
) -> Image.Image:
    """Card de texto: rótulo, título grande e uma lista com ícones (pontos fortes, pra quem é…)."""
    base = _fundo(paleta).convert("RGBA")
    _cabecalho(base, paleta, marca, posicao)
    lado_foto = 220
    largura_titulo = LARGURA - 2 * _MARGEM - (lado_foto + 30 if foto is not None else 0)
    if foto is not None:
        caixa = (LARGURA - _MARGEM - lado_foto, 140, LARGURA - _MARGEM, 140 + lado_foto)
        _sombra(base, caixa, 32, 40)
        _colar_arredondado(base, _foto_no_cartao(foto, (lado_foto, lado_foto), paleta.cartao), caixa[:2], 32)
    draw = ImageDraw.Draw(base)
    y = _rotulo(draw, _MARGEM, 150, rotulo, paleta) + 26
    fonte_t = _fonte("Black", 68)
    for linha in _quebrar(limpar_para_card(titulo).upper(), fonte_t, largura_titulo, 3):
        draw.text((_MARGEM, y), linha, font=fonte_t, fill=paleta.texto)
        y += int(_altura(fonte_t) * 0.86)
    y = max(y + 40, (140 + lado_foto + 50) if foto is not None else 0)

    itens = [limpar_para_card(i) for i in itens if limpar_para_card(i)]
    disponivel = ALTURA - 170 - y
    largura_txt = LARGURA - 2 * _MARGEM - 100
    for tam in (54, 50, 46, 42, 38, 35, 32):
        fonte = _fonte("SemiBold", tam)
        blocos = [_quebrar(i, fonte, largura_txt, 4) for i in itens]
        altura = sum(len(b) * _altura(fonte) * 1.02 for b in blocos) + 34 * (len(blocos) - 1)
        if altura <= disponivel:
            break
    y += max(0, (disponivel - altura) / 2.4)
    for bloco in blocos:
        _icone(draw, _MARGEM + 30, y + _altura(fonte) * 0.52, 30, icone, paleta)
        for linha in bloco:
            draw.text((_MARGEM + 90, y), linha, font=fonte, fill=paleta.texto)
            y += _altura(fonte) * 1.02
        y += 34
    _rodape_link(base, paleta)
    return base.convert("RGB")


def card_veredito(
    p: ProdutoCard, frase: str, paleta: Paleta, marca: str, posicao: str = "", vendas: Optional[int] = None
) -> Image.Image:
    base = _fundo(paleta).convert("RGBA")
    _cabecalho(base, paleta, marca, posicao)
    draw = ImageDraw.Draw(base)
    y = _rotulo(draw, _MARGEM, 150, "Veredito", paleta) + 30
    largura = LARGURA - 2 * _MARGEM
    for tam in (60, 54, 48, 44):
        fonte = _fonte("Bold", tam)
        linhas = _quebrar(limpar_para_card(frase), fonte, largura, 5)
        if len(linhas) * _altura(fonte) * 0.95 <= 420:
            break
    for linha in linhas:
        draw.text((_MARGEM, y), linha, font=fonte, fill=paleta.texto)
        y += int(_altura(fonte) * 0.95)

    lado = 400
    topo = ALTURA - 160 - lado
    caixa = (_MARGEM, topo, _MARGEM + lado, topo + lado)
    _sombra(base, caixa, 40, 45)
    _colar_arredondado(base, _foto_no_cartao(p.foto, (lado, lado), paleta.cartao), caixa[:2], 40)
    if p.desconto:
        _etiqueta(base, _MARGEM + 18, topo + 18, f"-{p.desconto}%", paleta)
    x = _MARGEM + lado + 40
    draw = ImageDraw.Draw(base)
    fim = _bloco_preco(draw, p, paleta, x, topo + 90, tam=80)
    if p.avaliacao:
        _nota(draw, p, paleta, x - 4, fim + 20)
        fim += 70
    if formatar_vendas(vendas):
        draw.text((x, fim + 20), formatar_vendas(vendas), font=_fonte("Medium", 34), fill=paleta.suave)
    _rodape_link(base, paleta)
    return base.convert("RGB")


def montar_vale_a_pena(
    p: ProdutoCard,
    nome: str,
    o_que_e: str,
    pontos_fortes: Sequence[str],
    atencao: Sequence[str],
    pra_quem: Sequence[str],
    veredito: str,
    marca: Optional[str] = None,
    variante: int = 0,
    dia: Optional[date] = None,
    vendas: Optional[int] = None,
) -> list[Image.Image]:
    """Capa "Vale a pena?" + o que é + pontos fortes + fica de olho + pra quem é + veredito + final."""
    marca = marca or marca_padrao()
    paleta = PALETAS[variante % len(PALETAS)]
    cards = [capa([p], paleta, marca, "Vale a pena?", limpar_para_card(nome), "destaque", dia)]
    paginas = [
        ("O que é", nome, [o_que_e], "seta"),
        ("Pontos fortes", "Por que chama atenção", pontos_fortes, "check"),
        ("Fica de olho", "Antes de comprar", atencao, "alerta"),
        ("Pra quem é", "Combina com você se…", pra_quem, "check"),
    ]
    paginas = [pg for pg in paginas if any(str(i).strip() for i in pg[2])]
    total = len(paginas) + 1
    for n, (rotulo, titulo, itens, icone) in enumerate(paginas, 1):
        cards.append(card_topicos(paleta, marca, rotulo, titulo, itens, icone, p.foto, f"{n}/{total}"))
    cards.append(card_veredito(p, veredito, paleta, marca, f"{total}/{total}", vendas))
    cards.append(card_final(paleta, marca))
    return cards


def card_meme(texto: str, paleta: Paleta, marca: Optional[str] = None) -> Image.Image:
    """Meme no formato de print de tweet, com a cara da página (sem curtidas nem números inventados)."""
    marca = marca or marca_padrao()
    base = _fundo(paleta).convert("RGBA")
    largura = LARGURA - 2 * _MARGEM
    pad = 56
    paragrafos = limpar_para_card(texto).splitlines()
    for tam in (60, 54, 48, 44, 40, 36):
        fonte = _fonte("Medium", tam)
        linhas: list[str] = []
        for paragrafo in paragrafos:
            linhas.extend(_quebrar(paragrafo, fonte, largura - 2 * pad, 6) if paragrafo else [""])
        altura_txt = sum(_altura(fonte) * (1.05 if linha else 0.55) for linha in linhas)
        if altura_txt <= 760:
            break
    altura_cartao = int(pad + 110 + 40 + altura_txt + pad)
    y0 = (ALTURA - altura_cartao) // 2
    caixa = (_MARGEM, y0, LARGURA - _MARGEM, y0 + altura_cartao)
    _sombra(base, caixa, 44, 55)
    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle(caixa, 44, fill="#FFFFFF")
    ax, ay = _MARGEM + pad, y0 + pad
    draw.ellipse((ax, ay, ax + 110, ay + 110), fill=paleta.destaque)
    inicial = (marca.lstrip("@")[:1] or "D").upper()
    fonte_ini = _fonte("Black", 60)
    draw.text((ax + 55 - fonte_ini.getlength(inicial) / 2, ay + 14), inicial, font=fonte_ini, fill=paleta.selo_texto)
    draw.text((ax + 140, ay + 12), "diquinhas & promos", font=_fonte("Bold", 40), fill="#14171A")
    arroba = marca if marca.startswith("@") else "@" + marca.replace(" ", "").lower()
    draw.text((ax + 140, ay + 62), arroba, font=_fonte("Regular", 32), fill="#657786")
    y = ay + 110 + 40
    for linha in linhas:
        if linha:
            draw.text((_MARGEM + pad, y), linha, font=fonte, fill="#14171A")
            y += _altura(fonte) * 1.05
        else:
            y += _altura(fonte) * 0.55
    return base.convert("RGB")


def montar_carrossel(
    produtos: Sequence[ProdutoCard],
    titulo: str,
    subtitulo: str = "",
    marca: Optional[str] = None,
    variante: int = 0,
    chamada: str = "Quer o link?",
    estilo: Optional[str] = None,
    dia: Optional[date] = None,
) -> list[Image.Image]:
    """
    Capa + um card por produto + card final. `variante` (ex.: um contador de posts) escolhe a
    paleta e, se `estilo` não for dado, também o estilo da capa, para os posts não saírem iguais.
    """
    if not produtos:
        raise ValueError("carrossel sem produtos")
    marca = marca or marca_padrao()
    paleta = PALETAS[variante % len(PALETAS)]
    # 5 paletas × 3 capas (primos entre si): 15 dias seguidos sem repetir a combinação
    estilo = estilo or ESTILOS_CAPA[variante % len(ESTILOS_CAPA)]
    total = len(produtos)
    cards = [capa(produtos, paleta, marca, titulo, subtitulo, estilo, dia)]
    cards += [card_produto(p, paleta, marca, f"{i}/{total}") for i, p in enumerate(produtos, 1)]
    cards.append(card_final(paleta, marca, chamada))
    return cards


def salvar_jpeg(img: Image.Image, destino: str | Path) -> Path:
    """O Instagram aceita JPEG; qualidade 90 fica bem abaixo do limite de 8 MB."""
    destino = Path(destino)
    img.save(destino, "JPEG", quality=90, optimize=True, progressive=True)
    return destino
