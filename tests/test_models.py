from models import ProdutoOferta


def _produto(**kwargs):
    base = {"titulo": "Gloss Labial", "preco_promocional": 29.9, "link_produto": "https://x.com/MLB-123"}
    return ProdutoOferta(**{**base, **kwargs})


def test_preco_original_menor_ou_igual_vira_none():
    assert _produto(preco_original=29.9).preco_original is None
    assert _produto(preco_original=10).preco_original is None
    assert _produto(preco_original=39.9).preco_original == 39.9


def test_campos_opcionais_sem_valor_inventado():
    p = _produto()
    assert p.preco_original is None
    assert p.avaliacao is None
    assert p.imagem_url is None


def test_id_extraido_do_link_e_imagem_invalida_descartada():
    p = _produto(imagem_url="nao-e-url")
    assert p.id == "MLB123"
    assert p.imagem_url is None


def test_titulo_limpo():
    assert _produto(titulo="Gloss <b>Top</b> | Mercado Livre").titulo == "Gloss Top"
