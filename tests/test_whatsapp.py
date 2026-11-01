from publishers.whatsapp import montar_texto_whatsapp, percentual_desconto


def test_texto_com_desconto_e_nota():
    texto = montar_texto_whatsapp(
        {
            "emojis": "🔥💸",
            "titulo": "Protetor *Solar* _FPS_ 70",
            "preco": 32.9,
            "preco_original": 59.82,
            "avaliacao": 4.9,
            "link": "https://s.shopee.com.br/abc",
        }
    )
    assert texto == (
        "🔥💸 *Protetor Solar FPS 70*\n"
        "⭐ Avaliação: 4,9/5\n"
        "\n"
        "❌ De: ~R$ 59,82~\n"
        "✅ *Por: R$ 32,90* (45% OFF)\n"
        "\n"
        "🛒 Compre aqui: https://s.shopee.com.br/abc"
    )


def test_texto_sem_dados_inventados():
    texto = montar_texto_whatsapp({"titulo": "Esmalte", "preco": 1234.5, "link": "https://x.com"})
    assert "De:" not in texto and "⭐" not in texto and "OFF" not in texto
    assert "✅ *Por: R$ 1.234,50*" in texto


def test_percentual_desconto():
    assert percentual_desconto(100, 75) == 25
    assert percentual_desconto(None, 75) is None
    assert percentual_desconto(75, 75) is None
