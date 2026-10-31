import pytest

from extractors import mercadolivre
from extractors.filtros import eh_produto_masculino, genero_permitido
from extractors.mercadolivre import MercadoLivreExtractor


@pytest.fixture(autouse=True)
def _limpa_cache():
    mercadolivre._CACHE_DESTAQUES.clear()
    mercadolivre._CACHE_PRODUTOS.clear()
    yield
    mercadolivre._CACHE_DESTAQUES.clear()
    mercadolivre._CACHE_PRODUTOS.clear()


@pytest.mark.parametrize(
    "titulo",
    [
        "Body Splash Masculino Vibration 200ml",
        "Attracione Men perfume Com Feromônios",
        "Philips OneBlade Aparador e Barbeador Elétrico",
        "Óleo para Barba 30ml",
        "Kit Pós-Barba Nivea",
        "Shampoo Homem 3 em 1",
        "Perfume Para Homens Ferrari",
        "Gel de Barbear Bozzano",
        "Barbeador Elétrico Recarregável 3 em 1",
        "Barbeador Masculino e Feminino",
        "Barbeador Feminino para Barba e Bigode",
        "Maquina Maquininha Dragao Acabamento Cortar Cabelo",
        "Desodorante MASCULINA edição limitada",
        "Colônia Infantil para Meninos",
    ],
)
def test_detecta_masculino(titulo):
    assert eh_produto_masculino(titulo)


@pytest.mark.parametrize(
    "titulo",
    [
        "Creme Multirreparador Cicaplast Baume B5+ La Roche-Posay",
        "L'Oréal Paris Elseve Óleo Extraordinário 100ml",
        "Aparador de Pelos Feminino Philips Satinelle",
        "Barbeador Elétrico Feminino 4 Em 1 Depilador Aparador Corpo Íntimo Facial Nariz USB Recarregável",
        "Lady Shaver Women Depilador",
        "Batom Mate Mari Maria Manteiga",
        "Esmalte Risqué Cremoso",
    ],
)
def test_aceita_feminino_ou_neutro(titulo):
    assert not eh_produto_masculino(titulo)


def test_genero_e_dominio_do_catalogo_tambem_contam():
    assert eh_produto_masculino("Perfume Kaiak 100ml", "MLB-PERFUMES", "Masculino")
    assert eh_produto_masculino("Gel 200g", "MLB-AFTERSHAVES")
    assert not eh_produto_masculino("Perfume Kaiak 100ml", "MLB-PERFUMES", "Feminino")


def test_genero_permitido():
    assert genero_permitido([])
    assert genero_permitido(["Feminino"])
    assert genero_permitido(["Unissex"])
    assert genero_permitido(["MULHER"])
    assert genero_permitido(["Sem gênero"])
    assert not genero_permitido(["Meninos"])
    assert not genero_permitido(["Bebês"])
    assert not genero_permitido(["Masculino"])


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def _attr_genero(valor):
    return [{"id": "GENDER", "value_name": valor}]


def test_api_get_espera_e_repete_no_429(monkeypatch):
    respostas = [_Resp({}, 429, {"Retry-After": "3"}), _Resp({}, 429), _Resp({"ok": True})]
    esperas = []
    monkeypatch.setattr(mercadolivre.requests, "get", lambda *a, **k: respostas.pop(0))
    monkeypatch.setattr(mercadolivre.time, "sleep", esperas.append)

    resp = mercadolivre._api_get("https://api/x", {})
    assert resp.json() == {"ok": True}
    assert esperas == [3.0, 4.0]


def test_mais_vendidos_usam_cache(monkeypatch):
    chamadas = []

    def fake_get(url, **kwargs):
        chamadas.append(url)
        if "/highlights/" in url:
            return _Resp({"content": [{"id": "MLB1", "type": "PRODUCT"}]})
        if url.endswith("/items"):
            return _Resp({"results": [{"price": 10.0, "original_price": 20.0}]})
        return _Resp({"name": "Gloss", "pictures": [{"url": "https://img"}], "attributes": []})

    monkeypatch.setattr(mercadolivre.requests, "get", fake_get)
    monkeypatch.setattr(MercadoLivreExtractor, "_obter_token_oficial", lambda self: "token")

    ml = MercadoLivreExtractor()
    assert len(ml.buscar_ofertas_mais_vendidos("MLB1248")) == 1
    assert len(ml.buscar_ofertas_mais_vendidos("MLB1248")) == 1
    # destaques e produto vêm do cache na segunda vez; o preço é sempre consultado
    assert sum("/highlights/" in u for u in chamadas) == 1
    assert sum(u.endswith("/products/MLB1") for u in chamadas) == 1
    assert sum(u.endswith("/items") for u in chamadas) == 2


def test_busca_por_termos_filtra_genero(monkeypatch):
    resultados = {
        "Floratta": [
            {"id": "MLB10", "name": "Floratta Rose", "pictures": [{"url": "https://img"}], "attributes": _attr_genero("Feminino")},
            {"id": "MLB11", "name": "Floratta Kids", "pictures": [{"url": "https://img"}], "attributes": _attr_genero("Meninos")},
        ],
        "Egeo": [
            {"id": "MLB10", "name": "Floratta Rose", "pictures": [{"url": "https://img"}], "attributes": _attr_genero("Feminino")},
            {"id": "MLB12", "name": "Egeo Dolce", "pictures": [{"url": "https://img"}], "attributes": _attr_genero("Unissex")},
        ],
    }
    precos = {"MLB10": (80.0, 100.0), "MLB11": (10.0, 50.0), "MLB12": (50.0, 100.0)}

    def fake_get(url, params=None, **kwargs):
        if url.endswith("/products/search"):
            return _Resp({"results": resultados[params["q"]]})
        pid = url.split("/products/")[1].split("/")[0]
        preco, de = precos[pid]
        return _Resp({"results": [{"price": preco, "original_price": de}]})

    monkeypatch.setattr(mercadolivre.requests, "get", fake_get)
    monkeypatch.setattr(MercadoLivreExtractor, "_obter_token_oficial", lambda self: "token")

    ofertas = MercadoLivreExtractor().buscar_ofertas_por_termos(["Floratta", "Egeo"])
    assert [o.id for o in ofertas] == ["MLB12", "MLB10"]  # Egeo -50% antes de Floratta -20%; Kids (Meninos) fora


def test_mais_vendidos_so_com_desconto_real_e_sem_masculinos(monkeypatch):
    produtos = {
        "MLB1": {"name": "Protetor Solar Anthelios", "pictures": [{"url": "https://img/1.jpg"}], "attributes": []},
        "MLB2": {"name": "Perfume Masculino Malbec", "pictures": [{"url": "https://img/2.jpg"}], "attributes": []},
        "MLB3": {"name": "Sérum Vitamina C", "pictures": [{"url": "https://img/3.jpg"}], "attributes": []},
        "MLB4": {"name": "Máscara Amend", "pictures": [{"url": "https://img/4.jpg"}], "attributes": []},
        "MLB5": {"name": "Gloss Labial", "pictures": [{"url": "https://img/5.jpg"}], "attributes": []},
    }
    anuncios = {
        "MLB1": [{"price": 77.17, "original_price": 109.9}, {"price": 106.9, "original_price": None}],
        "MLB2": [{"price": 50.0, "original_price": 100.0}],
        "MLB3": [{"price": 28.07, "original_price": None}],  # sem desconto: fica de fora
        "MLB4": [{"price": 29.38, "original_price": 60.0}],
        "MLB5": [{"price": 10.0, "original_price": 20.0}],  # já postado: ignorado
    }
    chamadas = []

    def fake_get(url, **kwargs):
        chamadas.append(url)
        if "/highlights/" in url:
            return _Resp({"content": [{"id": pid, "type": "PRODUCT"} for pid in produtos] + [{"id": "MLB9", "type": "ITEM"}]})
        pid = url.split("/products/")[1].split("/")[0]
        if url.endswith("/items"):
            return _Resp({"results": anuncios[pid]})
        return _Resp(produtos[pid])

    monkeypatch.setattr(mercadolivre.requests, "get", fake_get)
    monkeypatch.setattr(MercadoLivreExtractor, "_obter_token_oficial", lambda self: "token")

    ofertas = MercadoLivreExtractor().buscar_ofertas_mais_vendidos("MLB199407", ignorar=lambda pid: pid == "MLB5")

    # ordenado do maior desconto (Amend ~51%) para o menor (Anthelios ~30%)
    assert [o.id for o in ofertas] == ["MLB4", "MLB1"]
    assert ofertas[1].preco_original == 109.9 and ofertas[1].preco_promocional == 77.17
    assert ofertas[1].link_produto == "https://www.mercadolivre.com.br/protetor-solar-anthelios/p/MLB1"
    assert all(o.categoria == "ofertas" for o in ofertas)
    # masculino é descartado antes de consultar preços; o já postado nem é buscado
    assert not any(u.endswith("/MLB2/items") for u in chamadas)
    assert not any("/MLB5" in u for u in chamadas)
