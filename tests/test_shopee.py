from extractors.shopee import ShopeeExtractor


def test_parse_price():
    assert ShopeeExtractor._parse_price("R$ 1.299,90") == 1299.90
    assert ShopeeExtractor._parse_price("74,90") == 74.90
    assert ShopeeExtractor._parse_price("129.90") == 129.90
    assert ShopeeExtractor._parse_price("") is None
    assert ShopeeExtractor._parse_price("0") is None


def test_parse_sales():
    assert ShopeeExtractor._parse_sales("2mil+") == 2000
    assert ShopeeExtractor._parse_sales("1,5mil") == 1500
    assert ShopeeExtractor._parse_sales("466") == 466
    assert ShopeeExtractor._parse_sales(None) == 0


def test_extrair_de_csv_sem_dados_inventados(tmp_path):
    csv_file = tmp_path / "produtos.csv"
    csv_file.write_text(
        "Item Id,Item Name,Price,Sales,Offer Link\n"
        '1,Mascara Amend,"74,90",10,https://s.shopee.com.br/a\n'
        "2,Sem Preco,,500,https://s.shopee.com.br/b\n"
        '3,Shampoo Dove,"19,90",900,https://s.shopee.com.br/c\n',
        encoding="utf-8",
    )
    ofertas = ShopeeExtractor().extrair_de_csv(csv_file, resolver_imagens=False)

    assert [o["id"] for o in ofertas] == ["SHOPEE3", "SHOPEE1"]  # ordenado por vendas, sem a linha sem preço
    for oferta in ofertas:
        assert oferta["preco_original"] is None
        assert oferta["avaliacao"] is None
        assert oferta["imagem_url"] is None
        assert oferta["loja"] == "shopee"


def test_extrair_de_csv_calcula_de_a_partir_do_desconto_real(tmp_path):
    csv_file = tmp_path / "produtos.csv"
    csv_file.write_text(
        "Item Id,Item Name,Price,Discount,Offer Link\n"
        '1,Mascara Amend,"75,00",25%,https://s.shopee.com.br/a\n',
        encoding="utf-8",
    )
    [oferta] = ShopeeExtractor().extrair_de_csv(csv_file, resolver_imagens=False)
    assert oferta["preco_original"] == 100.0


def test_atualizar_com_api_usa_preco_atual_e_so_calcula_de_com_preco_unico(monkeypatch):
    ext = ShopeeExtractor(app_id="123", app_secret="segredo")
    respostas = {
        "1": {"itemId": 1, "price": "30", "priceMin": "30", "priceMax": "30", "priceDiscountRate": 40,
              "ratingStar": "4.87", "imageUrl": "https://cf.shopee.com.br/file/a"},
        "2": {"itemId": 2, "price": "10", "priceMin": "10", "priceMax": "50", "priceDiscountRate": 60,
              "ratingStar": "0", "imageUrl": ""},
    }
    monkeypatch.setattr(ext, "_consultar_lote_api", lambda ids: {str(i): respostas[str(i)] for i in ids if str(i) in respostas})
    base = {"preco_original": None, "imagem_url": None, "avaliacao": None, "categoria": "ofertas",
            "emojis": "🔥", "loja": "shopee", "marca": "ofertas"}
    ofertas = [
        {**base, "id": "SHOPEE1", "titulo": "Creme", "preco_promocional": 99.0, "link_produto": "https://s.shopee.com.br/a"},
        {**base, "id": "SHOPEE2", "titulo": "Kit", "preco_promocional": 99.0, "link_produto": "https://s.shopee.com.br/b"},
        {**base, "id": "SHOPEE3", "titulo": "Sumiu", "preco_promocional": 99.0, "link_produto": "https://s.shopee.com.br/c"},
    ]
    creme, kit = ext.atualizar_com_api(ofertas)

    assert (creme["preco_promocional"], creme["preco_original"], creme["avaliacao"]) == (30.0, 50.0, 4.9)
    assert creme["imagem_url"] == "https://cf.shopee.com.br/file/a"
    assert creme["link_produto"] == "https://s.shopee.com.br/a" and creme["marca"] == "ofertas"
    # com variações de preço não dá para saber a qual preço o desconto se refere: sem "De"
    assert (kit["preco_promocional"], kit["preco_original"], kit["avaliacao"], kit["imagem_url"]) == (10.0, None, None, None)


def test_atualizar_com_api_sem_credenciais_nao_altera(monkeypatch):
    ext = ShopeeExtractor()
    ext.app_id = ext.app_secret = ""  # "" no construtor cairia nas chaves do .env
    ofertas = [{"id": "SHOPEE1", "titulo": "Creme"}]
    assert ext.atualizar_com_api(ofertas) == ofertas
