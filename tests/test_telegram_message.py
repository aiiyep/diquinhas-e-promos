from publishers.telegram_publisher import _build_html_message, _format_brl


def test_format_brl():
    assert _format_brl(1234.5) == "1.234,50"


def test_titulo_e_link_escapados():
    msg = _build_html_message(
        {
            "titulo": "Kit Shampoo & Condicionador <500ml>",
            "preco_promocional": 49.9,
            "link_produto": 'https://x.com/p?a=1&b="2"',
        }
    )
    assert "Kit Shampoo &amp; Condicionador &lt;500ml&gt;" in msg
    assert 'href="https://x.com/p?a=1&amp;b=&quot;2&quot;"' in msg


def test_sem_de_e_sem_nota_quando_nao_ha_dados_reais():
    msg = _build_html_message({"titulo": "Gloss", "preco_promocional": 29.9, "link_produto": "https://x.com"})
    assert "De:" not in msg
    assert "Avaliação" not in msg
    assert "Por: R$ 29,90" in msg


def test_com_de_e_nota_reais():
    msg = _build_html_message(
        {
            "titulo": "Gloss",
            "preco_original": 39.9,
            "preco_promocional": 29.9,
            "avaliacao": 4.6,
            "link_produto": "https://x.com",
        }
    )
    assert "<s>R$ 39,90</s>" in msg
    assert "4,6/5" in msg
