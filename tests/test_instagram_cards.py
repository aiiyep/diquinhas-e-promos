from PIL import Image, ImageFont
import pytest

from publishers.instagram_cards import (
    ALTURA,
    ESTILOS_CAPA,
    LARGURA,
    PALETAS,
    ProdutoCard,
    _fonte,
    _quebrar,
    capa,
    montar_carrossel,
)


def _produto(**extra):
    foto = Image.new("RGB", (400, 300), "red")
    dados = {"titulo": "Batom Mate Longa Duração Cor Nude Rosado 3,5g", "preco_promocional": 19.9}
    return ProdutoCard.de_dict({**dados, **extra}, foto)


def test_desconto_so_com_de_real():
    assert _produto(preco_original=39.8).desconto == 50
    assert _produto().desconto is None
    assert _produto(preco_original=10.0).desconto is None  # "De" menor que o preço é ignorado


def test_produto_sem_preco_e_recusado():
    with pytest.raises(ValueError):
        ProdutoCard.de_dict({"titulo": "x"})


def test_foto_invalida_vira_cartao_vazio():
    p = ProdutoCard.de_dict({"titulo": "x", "preco": 10}, b"nao e imagem")
    assert p.foto is None


def test_carrossel_tem_capa_produtos_e_final():
    produtos = [_produto(preco_original=40.0), _produto(), _produto(avaliacao=4.8)]
    cards = montar_carrossel(produtos, "Top 3 achadinhos", "Beleza com desconto")
    assert len(cards) == 5
    assert all(c.size == (LARGURA, ALTURA) and c.mode == "RGB" for c in cards)


def test_variante_muda_paleta_e_capa():
    combinacoes = {
        (v % len(PALETAS), v % len(ESTILOS_CAPA)) for v in range(len(PALETAS) * len(ESTILOS_CAPA))
    }
    assert len(combinacoes) == len(PALETAS) * len(ESTILOS_CAPA)


@pytest.mark.parametrize("estilo", ESTILOS_CAPA)
def test_todos_os_estilos_de_capa(estilo):
    img = capa([_produto(preco_original=30.0)] * 4, PALETAS[0], "Marca", "Achado do dia", "sub", estilo)
    assert img.size == (LARGURA, ALTURA)


def test_quebrar_respeita_largura_e_linhas():
    fonte = _fonte("SemiBold", 40)
    texto = "palavra " * 40
    linhas = _quebrar(texto, fonte, 500, 2)
    assert len(linhas) == 2
    assert linhas[-1].endswith("…")
    assert all(fonte.getlength(l) <= 500 for l in linhas)
