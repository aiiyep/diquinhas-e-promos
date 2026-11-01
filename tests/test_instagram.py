from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from database.historico_ofertas import HistoricoDB
from orquestrador import instagram
from publishers import instagram_legenda
from publishers.instagram_legenda import gerar_textos

AGORA = datetime(2026, 11, 2, 10, 30)


def _c(i, preco, de=None, nota=None, titulo=None):
    return {
        "id_produto": f"SHOPEE{i}", "titulo": titulo or f"Cosmetico{i} Marca{i} Linha{i}",
        "preco": preco, "preco_original": de, "avaliacao": nota, "imagem_url": f"https://img/{i}.jpg",
        "link": f"https://s/{i}", "loja": "shopee",
    }


@pytest.fixture(autouse=True)
def _sem_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("INSTAGRAM_HORARIOS", raising=False)
    monkeypatch.delenv("INSTAGRAM_AUTO", raising=False)


# ---------------------------------------------------------------- seleção


def test_top5_so_com_desconto_do_maior_para_o_menor():
    cands = [_c(1, 50, 100), _c(2, 10), _c(3, 80, 100), _c(4, 10, 100), _c(5, 30, 40)]
    produtos, limite = instagram.selecionar("top5", cands)
    assert [p["id_produto"] for p in produtos] == ["SHOPEE4", "SHOPEE1", "SHOPEE5", "SHOPEE3"]
    assert produtos[0]["desconto"] == 90 and limite is None


def test_barato_usa_30_ou_50_e_exige_minimo():
    tres_baratos = [_c(1, 20, 40), _c(2, 25), _c(3, 29, 30), _c(4, 45, 90)]
    produtos, limite = instagram.selecionar("barato", tres_baratos)
    assert limite == 30 and {p["id_produto"] for p in produtos} == {"SHOPEE1", "SHOPEE2", "SHOPEE3"}

    poucos = [_c(1, 20, 40), _c(4, 45, 90), _c(5, 49)]
    produtos, limite = instagram.selecionar("barato", poucos)
    assert limite == 50 and len(produtos) == 3

    assert instagram.selecionar("barato", [_c(1, 20), _c(2, 200)])[0] == []


def test_achado_sem_desconto_bom_escolhe_a_melhor_nota():
    produtos, _ = instagram.selecionar("achado", [_c(1, 90, 100, 4.1), _c(2, 50, None, 4.9)])
    assert produtos[0]["id_produto"] == "SHOPEE2"
    produtos, _ = instagram.selecionar("achado", [_c(1, 50, 100, 4.1), _c(2, 50, None, 4.9)])
    assert produtos[0]["id_produto"] == "SHOPEE1"


def test_selecao_tira_masculino_e_titulos_quase_iguais():
    cands = [
        _c(1, 10, 100, titulo="Shampoo Tonalizante Escurecedor Cabelos Brancos 300ml"),
        _c(2, 10, 90, titulo="Shampoo Escurecedor Cabelos Brancos Tonalizante Colored"),
        _c(3, 10, 80, titulo="Máquina de Barbear Aparador Profissional"),
        _c(4, 10, 70, titulo="Batom Mate Longa Duração"),
        _c(5, 10, 60, titulo="Máscara de Cílios à Prova D'água"),
    ]
    produtos, _ = instagram.selecionar("top5", cands)
    assert [p["id_produto"] for p in produtos] == ["SHOPEE1", "SHOPEE4", "SHOPEE5"]


def test_horarios_configuraveis(monkeypatch):
    assert [(s.chave, h, m) for s, h, m in instagram.horarios()] == [("achado", 11, 30), ("barato", 17, 30), ("top5", 21, 0)]
    monkeypatch.setenv("INSTAGRAM_HORARIOS", "12:15, xx, 20:00")
    assert [(h, m) for _, h, m in instagram.horarios()] == [(12, 15), (17, 30), (20, 0)]


# ---------------------------------------------------------------- textos


PRODUTOS = [
    {"titulo": "Pó Compacto Glass By Ruby Rose Cor 01", "preco": 14.98, "preco_original": 41.61, "desconto": 64,
     "avaliacao": 4.9},
    {"titulo": "Batom Líquido Matte Vult 6ml", "preco": 19.9, "preco_original": None, "desconto": None},
    {"titulo": "Kit 15 Pincéis de Maquiagem", "preco": 12.38, "preco_original": 30.2, "desconto": 59},
]


def test_textos_sem_ia_usam_o_padrao():
    t = gerar_textos("top5", PRODUTOS, AGORA)
    assert not t.usou_ia
    assert t.titulo_capa == "Top 3 achadinhos de hoje" and t.subtitulo == "Até 64% OFF"
    assert "1. Pó Compacto Glass By Ruby Rose Cor 01: R$ 14,98 (era R$ 41,61, 64% OFF)" in t.legenda
    assert "2. Batom Líquido Matte Vult 6ml: R$ 19,90\n" in t.legenda
    assert "Preços conferidos em 02/11 às 10:30" in t.legenda
    assert len(t.legenda) <= 2200


def test_textos_da_ia_sao_conferidos(monkeypatch):
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: {
        "titulo_capa": "Até 90% OFF hoje",  # 90 não é desconto de nenhum produto
        "subtitulo": "Com frete grátis",  # promessa que o bot não sabe
        "nomes_curtos": ["Po Compacto Glass", "Batom Vermelho Vult", "Kit 15 Pinceis"],
        "abertura": "Achei esses com até 64% OFF! 💖",
        "hashtags": ["#Maquiagem", "beleza barata", "#achadinhos"],
    })
    t = gerar_textos("top5", PRODUTOS, AGORA)
    assert t.usou_ia
    assert t.titulo_capa == "Top 3 achadinhos de hoje" and t.subtitulo == "Até 64% OFF"
    assert t.nomes[0] == "Pó Compacto Glass"  # acento devolvido pela grafia do título
    assert t.nomes[1] == "Batom Líquido Matte Vult 6ml"  # "Vermelho" não está no título: recusado
    assert t.nomes[2] == "Kit 15 Pincéis"
    assert t.legenda.startswith("Achei esses com até 64% OFF! 💖")
    assert "#maquiagem #belezabarata #achadinhos" in t.legenda
    assert set(t.recusados) == {"titulo_capa", "subtitulo", "nome 2"}


def test_textos_quando_o_gemini_falha(monkeypatch):
    def falha(*a, **k):
        raise instagram_legenda.GeminiIndisponivel("HTTP 503")

    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", falha)
    t = gerar_textos("barato", PRODUTOS, AGORA, limite=30)
    assert not t.usou_ia and t.titulo_capa == "Abaixo de R$ 30"


# ---------------------------------------------------------------- preparo e publicação


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (60, 60), "pink").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def db(tmp_path, monkeypatch):
    banco = HistoricoDB(tmp_path / "h.db")
    for i, (preco, de) in enumerate([(10, 100), (20, 80), (30, 60), (15, 30), (40, 50), (9, 10)], 1):
        banco.registrar_postagem(
            f"SHOPEE{i}", "Ofertas", titulo=f"Produto {['Batom', 'Sérum', 'Base', 'Gloss', 'Blush', 'Rímel'][i - 1]} Bonito",
            preco=preco, link=f"https://s/{i}", loja="shopee", preco_original=de, imagem_url=f"https://img/{i}.jpg",
        )
    monkeypatch.setattr(instagram, "PASTA_CARDS", tmp_path / "cards")
    monkeypatch.setattr(instagram, "atualizar_precos", lambda cands: cands)
    monkeypatch.setattr(instagram, "_baixar_foto", lambda url: _png())
    return banco


def test_preparar_post_grava_cards_e_nao_repete_produto(db):
    agendado = datetime(2026, 11, 2, 21, 0)
    pid = instagram.preparar_post(db, "top5", agendado, agora=AGORA)
    post = db.post_instagram(pid)
    assert post["status"] == "pendente" and post["n_cards"] == 7  # capa + 5 + final
    assert sorted(Path(post["pasta"]).iterdir())[0].name == "00.jpg"
    assert [p["id"] for p in post["produtos"]] == ["SHOPEE1", "SHOPEE2", "SHOPEE3", "SHOPEE4", "SHOPEE5"]

    # o que já está num carrossel ativo não volta: sobra 1 produto, menos que o mínimo de 3
    assert instagram.preparar_post(db, "top5", agendado, agora=AGORA) is None

    # "Gerar outro" troca os produtos; sem produtos suficientes, o antigo fica como está
    assert instagram.regerar_post(db, pid) is None
    assert db.post_instagram(pid)["status"] == "pendente"


def test_preparar_do_horario_nao_duplica(db):
    agora = datetime(2026, 11, 2, 20, 0, tzinfo=instagram._TZ)
    primeiro = instagram.preparar_do_horario(db, "top5", agora)
    assert instagram.preparar_do_horario(db, "top5", agora) == primeiro


def test_publicar_do_horario(db, monkeypatch):
    agora = datetime(2026, 11, 2, 21, 0, tzinfo=instagram._TZ)
    publicados = []
    monkeypatch.setattr(instagram, "publicar_post", lambda banco, post: publicados.append(post["id"]))

    pid = instagram.preparar_do_horario(db, "top5", agora)
    instagram.publicar_do_horario(db, "top5", agora)
    assert db.post_instagram(pid)["status"] == "expirado" and publicados == []

    db.atualizar_post_instagram(pid, "aprovado")
    instagram.publicar_do_horario(db, "top5", agora)
    assert publicados == [pid]


def test_publicacao_automatica_publica_pendente(db, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_AUTO", "1")
    agora = datetime(2026, 11, 2, 21, 0, tzinfo=instagram._TZ)
    publicados = []
    monkeypatch.setattr(instagram, "publicar_post", lambda banco, post: publicados.append(post["id"]))
    pid = instagram.preparar_do_horario(db, "top5", agora)
    instagram.publicar_do_horario(db, "top5", agora)
    assert publicados == [pid]


def test_status_invalido_e_recusado(db):
    with pytest.raises(ValueError):
        db.atualizar_post_instagram(1, "sumiu")


def test_jobs_do_instagram_no_agendador():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from orquestrador.agendador import configurar_agendador

    sched = configurar_agendador(AsyncIOScheduler(timezone="America/Sao_Paulo"))
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs() if j.id.startswith("instagram_")}
    assert jobs == {
        "instagram_preparar_achado": "cron[hour='10', minute='30']",
        "instagram_publicar_achado": "cron[hour='11', minute='30']",
        "instagram_preparar_barato": "cron[hour='16', minute='30']",
        "instagram_publicar_barato": "cron[hour='17', minute='30']",
        "instagram_preparar_top5": "cron[hour='20', minute='0']",
        "instagram_publicar_top5": "cron[hour='21', minute='0']",
    }
