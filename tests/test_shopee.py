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
