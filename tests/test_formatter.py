from formatters.promo_formatter import PromoFormatter


def test_link_mercado_livre_recebe_parametros(monkeypatch):
    monkeypatch.setenv("ML_MATT_WORD", "palavra")
    monkeypatch.setenv("ML_MATT_TOOL", "123")
    url = PromoFormatter().gerar_link_afiliado_url("https://www.mercadolivre.com.br/gloss/p/MLB1?x=1#frag")
    assert url == "https://www.mercadolivre.com.br/gloss/p/MLB1?x=1&matt_word=palavra&matt_tool=123"


def test_link_curto_preservado():
    assert PromoFormatter().gerar_link_afiliado_url(" https://s.shopee.com.br/abc ") == "https://s.shopee.com.br/abc"


def test_aplicar_afiliado_calcula_desconto():
    out = PromoFormatter().aplicar_afiliado({"preco_original": 100.0, "preco_promocional": 75.0})
    assert out["desconto_pct"] == 25
    out = PromoFormatter().aplicar_afiliado({"preco_original": None, "preco_promocional": 75.0})
    assert "desconto_pct" not in out
