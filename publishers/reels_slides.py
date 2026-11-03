"""
Slides em pé (1080×1920) dos Reels e o vídeo MP4 montado com eles, sem áudio.

O Reels é semiautomático: o bot monta o vídeo e o usuário posta pelo app do Instagram, onde escolhe
uma música em alta (a API não tem acesso à biblioteca de músicas). Por isso o vídeo sai mudo.

Os slides não mostram preço nem desconto: o Reels continua sendo visto por meses e o preço muda.
Preço e link ficam no canal do WhatsApp ("link na bio").

Zona segura do Reels: o Instagram cobre ~250 px do topo e ~420 px de baixo (legenda e botões) e a
coluna da direita (curtir, comentar). O conteúdo fica entre y 260 e 1500.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageOps

from publishers.instagram_cards import (
    Paleta,
    _altura,
    _colar_arredondado,
    _fonte,
    _foto_no_cartao,
    _quebrar,
    _rgb,
    _sombra,
    _texto_centro,
    limpar_para_card,
    marca_padrao,
)

LARGURA, ALTURA = 1080, 1920
MARGEM = 80
TOPO_SEGURO = 260
BASE_SEGURA = 1500

FPS = 30
TRANSICAO = 0.25  # segundos de fusão entre um slide e o próximo


@dataclass
class Slide:
    imagem: Image.Image
    segundos: float


@dataclass
class ItemReel:
    """Um produto no slide: nome curto e foto já aberta."""

    nome: str
    foto: Image.Image


# ---------------------------------------------------------------- desenho


def _fundo(paleta: Paleta) -> Image.Image:
    vertical = Image.linear_gradient("L").resize((LARGURA, ALTURA))
    horizontal = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90).resize((LARGURA, ALTURA))
    mascara = Image.blend(vertical, ImageOps.mirror(horizontal), 0.35)
    return Image.composite(
        Image.new("RGB", (LARGURA, ALTURA), _rgb(paleta.fundo2)),
        Image.new("RGB", (LARGURA, ALTURA), _rgb(paleta.fundo1)),
        mascara,
    ).convert("RGBA")


def _pilula(draw: ImageDraw.ImageDraw, y: int, texto: str, paleta: Paleta, tam: int = 36) -> int:
    fonte = _fonte("Bold", tam)
    texto = limpar_para_card(texto).upper()
    largura = fonte.getlength(texto) + 64
    x = (LARGURA - largura) / 2
    alto = _altura(fonte) + 18
    draw.rounded_rectangle((x, y, x + largura, y + alto), alto // 2, fill=paleta.destaque)
    draw.text((x + 32, y + 9), texto, font=fonte, fill=paleta.selo_texto)
    return y + alto


def _marca(draw: ImageDraw.ImageDraw, paleta: Paleta, marca: str, direita: str = "") -> None:
    fonte = _fonte("SemiBold", 32)
    y = TOPO_SEGURO
    draw.ellipse((MARGEM, y + 12, MARGEM + 18, y + 30), fill=paleta.destaque)
    draw.text((MARGEM + 32, y), marca, font=fonte, fill=paleta.texto)
    if direita:
        draw.text((LARGURA - MARGEM - fonte.getlength(direita), y), direita, font=fonte, fill=paleta.suave)


def _bloco_titulo(draw: ImageDraw.ImageDraw, texto: str, paleta: Paleta, y: int, max_altura: int,
                  tamanhos: Sequence[int] = (132, 118, 104, 92, 80)) -> int:
    """Título grande em caixa alta, no maior tamanho que couber em `max_altura`."""
    largura = LARGURA - 2 * MARGEM
    for tam in tamanhos:
        fonte = _fonte("Black", tam)
        linhas = _quebrar(limpar_para_card(texto).upper(), fonte, largura, 6)
        if len(linhas) * _altura(fonte) * 0.86 <= max_altura:
            break
    for linha in linhas:
        y = _texto_centro(draw, int(y), linha, fonte, paleta.texto) - int(_altura(fonte) * 0.14)
    return y


def slide_gancho(paleta: Paleta, titulo: str, rotulo: str, marca: Optional[str] = None,
                 subtitulo: str = "") -> Image.Image:
    """Primeiro slide: só texto grande. É o que decide se a pessoa continua assistindo."""
    base = _fundo(paleta)
    draw = ImageDraw.Draw(base)
    _marca(draw, paleta, marca or marca_padrao())
    # mede para centralizar o bloco (rótulo + título + subtítulo) na zona segura
    largura = LARGURA - 2 * MARGEM
    for tam in (132, 118, 104, 92, 80):
        fonte = _fonte("Black", tam)
        linhas = _quebrar(limpar_para_card(titulo).upper(), fonte, largura, 6)
        if len(linhas) * _altura(fonte) * 0.86 <= 760:
            break
    fonte_sub = _fonte("Medium", 46)
    linhas_sub = _quebrar(limpar_para_card(subtitulo), fonte_sub, largura, 2) if subtitulo else []
    altura = 70 + 60 + len(linhas) * _altura(fonte) * 0.86 + (40 + len(linhas_sub) * _altura(fonte_sub) if linhas_sub else 0)
    y = int(TOPO_SEGURO + 80 + (BASE_SEGURA - TOPO_SEGURO - 80 - altura) / 2)
    y = _pilula(draw, y, rotulo, paleta) + 60
    for linha in linhas:
        y = _texto_centro(draw, int(y), linha, fonte, paleta.texto) - int(_altura(fonte) * 0.14)
    if linhas_sub:
        y += 40
        for linha in linhas_sub:
            y = _texto_centro(draw, int(y), linha, fonte_sub, paleta.suave)
    return base.convert("RGB")


def _cartao_foto(base: Image.Image, foto: Image.Image, caixa: tuple[int, int, int, int], paleta: Paleta) -> None:
    x0, y0, x1, y1 = caixa
    _sombra(base, caixa, 40, 50)
    _colar_arredondado(base, _foto_no_cartao(foto, (x1 - x0, y1 - y0), paleta.cartao), (x0, y0), 40)


def slide_produto(paleta: Paleta, rotulo: str, item: ItemReel, posicao: str = "", marca: Optional[str] = None) -> Image.Image:
    """Rótulo do passo/categoria, foto grande do produto e o nome curto embaixo."""
    base = _fundo(paleta)
    draw = ImageDraw.Draw(base)
    _marca(draw, paleta, marca or marca_padrao(), posicao)
    y = _pilula(draw, TOPO_SEGURO + 110, rotulo, paleta, 40) + 50
    lado = 820
    x0 = (LARGURA - lado) // 2
    _cartao_foto(base, item.foto, (x0, y, x0 + lado, y + lado), paleta)
    y += lado + 50
    fonte = _fonte("Bold", 56)
    for linha in _quebrar(limpar_para_card(item.nome), fonte, LARGURA - 2 * MARGEM, 2):
        y = _texto_centro(draw, y, linha, fonte, paleta.texto)
    return base.convert("RGB")


def slide_duelo(paleta: Paleta, rotulo: str, a: ItemReel, b: ItemReel, posicao: str = "",
                marca: Optional[str] = None) -> Image.Image:
    """"Isso ou aquilo": dois produtos do mesmo tipo lado a lado, numerados 1 e 2."""
    base = _fundo(paleta)
    draw = ImageDraw.Draw(base)
    _marca(draw, paleta, marca or marca_padrao(), posicao)
    y = _pilula(draw, TOPO_SEGURO + 100, rotulo, paleta, 40) + 30
    y = _texto_centro(draw, y, "1 OU 2?", _fonte("Black", 120), paleta.texto) + 10
    gap = 36
    largura = (LARGURA - 2 * MARGEM - gap) // 2
    altura = 560
    fonte_num = _fonte("Black", 64)
    fonte_nome = _fonte("SemiBold", 38)
    for i, item in enumerate((a, b)):
        x0 = MARGEM + i * (largura + gap)
        _cartao_foto(base, item.foto, (x0, y, x0 + largura, y + altura), paleta)
        cx, cy, r = x0 + 62, y + 62, 46
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=paleta.destaque)
        num = str(i + 1)
        draw.text((cx - fonte_num.getlength(num) / 2, cy - _altura(fonte_num) / 2 + 2), num, font=fonte_num,
                  fill=paleta.selo_texto)
        yn = y + altura + 26
        for linha in _quebrar(limpar_para_card(item.nome), fonte_nome, largura, 3):
            yn = _texto_centro(draw, yn, linha, fonte_nome, paleta.texto, x0, x0 + largura)
    return base.convert("RGB")


def slide_final(paleta: Paleta, titulo: str, linhas: Sequence[str], marca: Optional[str] = None) -> Image.Image:
    """Chamada final: os links ficam no canal do WhatsApp (link na bio)."""
    base = _fundo(paleta)
    draw = ImageDraw.Draw(base)
    _marca(draw, paleta, marca or marca_padrao())
    fonte_titulo = _fonte("Black", 112)
    n_titulo = len(_quebrar(limpar_para_card(titulo).upper(), fonte_titulo, LARGURA - 2 * MARGEM, 3))
    altura = n_titulo * _altura(fonte_titulo) * 0.86 + 90 + len(linhas) * 150
    y = int(TOPO_SEGURO + 80 + (BASE_SEGURA - TOPO_SEGURO - 80 - altura) / 2)
    y = _bloco_titulo(draw, titulo, paleta, y, 420, (112, 100, 90, 80))
    y += 90
    fonte_num = _fonte("ExtraBold", 48)
    fonte_txt = _fonte("SemiBold", 48)
    for n, texto in enumerate(linhas, 1):
        draw.ellipse((MARGEM + 20, y, MARGEM + 108, y + 88), fill=paleta.destaque)
        draw.text((MARGEM + 64 - fonte_num.getlength(str(n)) / 2, y + 12), str(n), font=fonte_num, fill=paleta.selo_texto)
        for i, linha in enumerate(_quebrar(limpar_para_card(texto), fonte_txt, LARGURA - 2 * MARGEM - 150, 2)):
            draw.text((MARGEM + 140, y + 12 + i * _altura(fonte_txt)), linha, font=fonte_txt, fill=paleta.texto)
        y += 150
    return base.convert("RGB")


# ---------------------------------------------------------------- vídeo


def quadros(slides: Sequence[Slide], fps: int = FPS, transicao: float = TRANSICAO):
    """Gera os quadros (bytes RGB) do vídeo: cada slide parado e uma fusão curta para o próximo."""
    n_fusao = max(0, int(round(transicao * fps)))
    for i, slide in enumerate(slides):
        atual = slide.imagem.convert("RGB")
        proximo = slides[i + 1].imagem.convert("RGB") if i + 1 < len(slides) else None
        total = max(1, int(round(slide.segundos * fps)))
        parados = total - (n_fusao if proximo is not None else 0)
        bruto = atual.tobytes()
        for _ in range(max(1, parados)):
            yield bruto
        if proximo is not None:
            for k in range(1, n_fusao + 1):
                yield Image.blend(atual, proximo, k / (n_fusao + 1)).tobytes()


def montar_video(slides: Sequence[Slide], destino: str | Path, fps: int = FPS) -> Path:
    """MP4 H.264 9:16, sem áudio (a música entra pelo app do Instagram)."""
    import imageio_ffmpeg

    if not slides:
        raise ValueError("Reels sem slides")
    destino = Path(destino)
    escritor = imageio_ffmpeg.write_frames(
        str(destino), (LARGURA, ALTURA), fps=fps, codec="libx264", quality=None, macro_block_size=8,
        output_params=["-preset", "veryfast", "-crf", "21", "-x264-params", "rc-lookahead=10",
                       "-movflags", "+faststart"],
    )
    escritor.send(None)  # inicia o ffmpeg
    try:
        for quadro in quadros(slides, fps):
            escritor.send(quadro)
    finally:
        escritor.close()
    return destino


def duracao(slides: Sequence[Slide]) -> float:
    return round(sum(s.segundos for s in slides), 1)
