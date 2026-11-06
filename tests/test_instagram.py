from datetime import datetime
from html import escape
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
    monkeypatch.setenv("META_MIDIA_URL", "")  # API do Instagram desligada, salvo nos testes dela


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
    assert [(s.chave, h, m) for s, h, m in instagram.horarios()] == [("valeapena", 11, 30), ("dica", 17, 30), ("top5", 21, 0)]
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
        "hashtags": ["#Maquiagem", "beleza barata", "#achadinhos", "#make", "#batom", "#base"],
    })
    t = gerar_textos("top5", PRODUTOS, AGORA)
    assert t.usou_ia
    assert t.titulo_capa == "Top 3 achadinhos de hoje" and t.subtitulo == "Até 64% OFF"
    assert t.nomes[0] == "Pó Compacto Glass"  # acento devolvido pela grafia do título
    assert t.nomes[1] == "Batom Líquido Matte Vult 6ml"  # "Vermelho" não está no título: recusado
    assert t.nomes[2] == "Kit 15 Pincéis"
    assert t.legenda.startswith("Achei esses com até 64% OFF! 💖")
    assert "#maquiagem #belezabarata #achadinhos" in t.legenda
    assert t.legenda.count("#") == 5  # 4 do nicho + #publi: o limite do Instagram
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


def test_aprovado_sem_api_avisa_no_privado_com_capa_e_legenda(db):
    agora = datetime(2026, 11, 2, 21, 0, tzinfo=instagram._TZ)
    pid = instagram.preparar_do_horario(db, "top5", agora)
    db.atualizar_post_instagram(pid, "aprovado")

    avisos = instagram.publicar_do_horario(db, "top5", agora)
    (instrucoes, capa), (legenda, sem_foto) = avisos
    assert "<b>Instagram 21:00: hora de postar</b>" in instrucoes and "Top do dia" in instrucoes
    assert "não está conectada, então ele não saiu sozinho" in instrucoes and capa.name == "00.jpg" and capa.is_file()
    post = db.post_instagram(pid)
    assert sem_foto is None and legenda == escape(post["legenda"])
    assert post["status"] == "aprovado"  # continua no painel até "Marquei como publicado"


def test_pendente_que_expira_nao_gera_aviso_de_postar(db):
    agora = datetime(2026, 11, 2, 21, 0, tzinfo=instagram._TZ)
    instagram.preparar_do_horario(db, "top5", agora)
    assert instagram.publicar_do_horario(db, "top5", agora) == []


def test_sem_aprovacao_nada_sai_nem_com_a_antiga_chave_auto(db, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_AUTO", "1")  # não existe mais: pendente continua expirando
    agora = datetime(2026, 11, 2, 21, 0, tzinfo=instagram._TZ)
    publicados = []
    monkeypatch.setattr(instagram, "publicar_post", lambda banco, post: publicados.append(post["id"]))
    pid = instagram.preparar_do_horario(db, "top5", agora)
    instagram.publicar_do_horario(db, "top5", agora)
    assert publicados == [] and db.post_instagram(pid)["status"] == "expirado"


def test_aviso_de_30_minutos(db):
    agora = datetime(2026, 11, 2, 20, 30, tzinfo=instagram._TZ)
    # nada preparado: avisa que não deu
    texto, capa = instagram.aviso_do_horario(db, "top5", agora)
    assert "não consegui montar" in texto and capa is None

    pid = instagram.preparar_do_horario(db, "top5", agora)
    texto, capa = instagram.aviso_do_horario(db, "top5", agora)
    assert "<b>Instagram 21:00</b>" in texto and "Top do dia" in texto and capa.name == "00.jpg" and capa.is_file()

    db.atualizar_post_instagram(pid, "aprovado")
    assert instagram.aviso_do_horario(db, "top5", agora) is None


def test_avisar_admin_sem_configuracao(monkeypatch):
    import asyncio
    from publishers import telegram_avisos

    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "")
    assert asyncio.run(telegram_avisos.avisar_admin("oi")) is False

    monkeypatch.setenv("PAINEL_URL", "https://painel.exemplo.ts.net/")
    teclado = telegram_avisos._botao_painel("#instagram")
    assert teclado.inline_keyboard[0][0].url == "https://painel.exemplo.ts.net/#instagram"
    monkeypatch.setenv("PAINEL_URL", "http://100.1.2.3:8080")
    assert telegram_avisos._botao_painel() is None


def test_status_invalido_e_recusado(db):
    with pytest.raises(ValueError):
        db.atualizar_post_instagram(1, "sumiu")


def test_jobs_do_instagram_no_agendador():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from orquestrador.agendador import configurar_agendador

    sched = configurar_agendador(AsyncIOScheduler(timezone="America/Sao_Paulo"))
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs() if j.id.startswith("instagram_")}
    assert jobs == {
        "instagram_preparar_valeapena": "cron[hour='10', minute='30']",
        "instagram_avisar_valeapena": "cron[hour='11', minute='0']",
        "instagram_avisar_dica": "cron[hour='17', minute='0']",
        "instagram_avisar_top5": "cron[hour='20', minute='30']",
        "instagram_publicar_valeapena": "cron[hour='11', minute='30']",
        "instagram_preparar_dica": "cron[hour='16', minute='30']",
        "instagram_publicar_dica": "cron[hour='17', minute='30']",
        "instagram_preparar_top5": "cron[hour='20', minute='0']",
        "instagram_publicar_top5": "cron[hour='21', minute='0']",
    }


# ---------------------------------------------------------------- "Vale a pena?" e dica

from publishers.instagram_legenda import ESTILOS_DICA, gerar_dica, gerar_vale_a_pena, tema_do_dia  # noqa: E402

PRODUTO_VALE = {
    "id_produto": "MLB1", "titulo": "Truss Uso Obrigatório Reconstrutor Capilar 260ml", "preco": 89.9,
    "preco_original": 120.0, "desconto": 25, "avaliacao": 4.8, "vendas": 12500,
    "detalhes": {"descricao": "Reconstrutor capilar que protege do calor do secador.",
                 "atributos": ["Volume líquido: 260 mL", "Tipos de cabelo: Danificados, Com frizz"]},
}

RESPOSTA_VALE = {
    "informacao_suficiente": True,
    "nome_curto": "Truss Uso Obrigatorio",
    "o_que_e": "Um reconstrutor capilar de 260 ml pra cabelo danificado.",
    "pontos_fortes": ["Nota 4,8 com mais de 12 mil vendidos", "Protege do calor do secador", "Eu testei e amei"],
    "atencao": ["Vem só 260 ml, rende pouco se o cabelo é longo"],
    "pra_quem": ["Quem tem cabelo danificado", "Quem vive de secador"],
    "veredito": "Vale a pena se seu cabelo pede reconstrução e você usa calor toda semana.",
    "abertura": "Miga, será que vale? 👀",
    "hashtags": ["#truss", "#cabelo"],
}


def test_vale_a_pena_confere_o_texto_da_ia(monkeypatch):
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: dict(RESPOSTA_VALE))
    t = gerar_vale_a_pena(PRODUTO_VALE, AGORA)
    assert t.nome == "Truss Uso Obrigatório"
    assert t.pontos_fortes == ["Nota 4,8 com mais de 12 mil vendidos", "Protege do calor do secador"]  # relato pessoal sai
    assert "💰 R$ 89,90 (era R$ 120,00, 25% OFF)" in t.legenda
    assert "⭐ nota 4,8 • +12 mil vendidos" in t.legenda
    assert "#publi" in t.legenda


@pytest.mark.parametrize("campo, valor", [
    ("veredito", "Vale a pena: garante cabelo novo em 7 dias"),  # promessa + número inventado
    ("o_que_e", "Reconstrutor que custa R$ 89"),  # preço só pelo código
    ("pra_quem", ["Eu uso no meu cabelo"]),
])
def test_vale_a_pena_recusa_texto_inventado(monkeypatch, campo, valor):
    chamadas = []
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: chamadas.append(1) or {**RESPOSTA_VALE, campo: valor})
    assert gerar_vale_a_pena(PRODUTO_VALE, AGORA) is None
    assert len(chamadas) == 2  # pediu de novo antes de desistir


def test_vale_a_pena_sem_dados_ou_sem_gemini(monkeypatch):
    assert gerar_vale_a_pena(PRODUTO_VALE, AGORA) is None  # sem chave
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: {"informacao_suficiente": False})
    assert gerar_vale_a_pena(PRODUTO_VALE, AGORA) is None


def test_ordem_do_vale_a_pena_prefere_quem_tem_prova():
    cands = [
        _c(1, 10, 100, nota=3.9),  # 90% OFF, mas nota baixa e sem vendas
        {**_c(2, 70, 100, nota=4.8), "vendas": 3000},
        {**_c(3, 50, 100, nota=4.2), "id_produto": "MLB3"},  # ficha do ML conta como prova
    ]
    assert [p["id_produto"] for p in instagram.ordenar_vale_a_pena(cands)] == ["MLB3", "SHOPEE2", "SHOPEE1"]


def test_sem_gemini_dica_e_vale_a_pena_usam_reserva(db, monkeypatch):
    # sem Gemini, a dica vira "Abaixo de R$ X" e o "Vale a pena?" vira "Achado do dia"
    dica = db.post_instagram(instagram.preparar_post(db, "dica", datetime(2026, 11, 2, 17, 30), agora=AGORA))
    assert dica["slot"] == "dica" and dica["formato"] == "barato"

    monkeypatch.setattr(instagram, "detalhes_produto", lambda p: {})
    vap = db.post_instagram(instagram.preparar_post(db, "valeapena", datetime(2026, 11, 2, 11, 30), agora=AGORA))
    assert vap["slot"] == "valeapena" and vap["formato"] == "achado"


def test_preparar_vale_a_pena_com_ia(db, monkeypatch):
    monkeypatch.setattr(instagram, "detalhes_produto", lambda p: {})
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    resposta = {**RESPOSTA_VALE, "nome_curto": "Produto Batom", "o_que_e": "Um batom bonito.",
                "atencao": ["Confira a cor antes de comprar"],
                "pontos_fortes": ["Cor bonita", "Preço bom"], "veredito": "Vale a pena se você quer um batom."}
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: resposta)
    pid = instagram.preparar_post(db, "valeapena", datetime(2026, 11, 2, 11, 30), agora=AGORA)
    post = db.post_instagram(pid)
    assert post["formato"] == "valeapena" and post["titulo"] == "Vale a pena? Produto Batom"
    assert len(post["produtos"]) == 1 and post["n_cards"] == 7  # capa, 4 tópicos, veredito, final


def test_travas_de_acento_dois_pontos_e_decimal(monkeypatch):
    from publishers.instagram_legenda import _sem_acento, _virgula_decimal

    assert _sem_acento("eu: este mes nao vou comprar") and not _sem_acento("eu: este mês não vou comprar")
    assert _virgula_decimal("nota 4.9 de 5") == "nota 4,9 de 5"

    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    resposta = {**RESPOSTA_VALE, "pontos_fortes": ["Nota 4.8 de quem comprou", "Protege do calor", "Nao pesa no cabelo"]}
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: dict(resposta))
    t = gerar_vale_a_pena(PRODUTO_VALE, AGORA)
    assert t.pontos_fortes == ["Nota 4,8 de quem comprou", "Protege do calor"]


RESPOSTA_DICA = {
    "titulo_capa": "3 erros que estragam sua base",
    "subtitulo": "O terceiro quase todo mundo comete",
    "itens": [
        {"titulo": "Pular o hidratante", "texto": "Pele sem hidratação deixa a base craquelada ao longo do dia."},
        {"titulo": "Passar demais", "texto": "Comece com pouco produto e vá construindo a cobertura."},
        {"titulo": "Eu uso esponja seca", "texto": "Relato pessoal: fora."},
        {"titulo": "Esquecer o pescoço", "texto": "Esfume a base até o pescoço para não marcar a divisão."},
        {"titulo": "Base cura espinhas", "texto": "Promessa: fora."},
    ],
    "busca": ["esponja", "Hidratante facial", "x"],
    "abertura": "Sua base não fica bonita? Pode ser um desses 👀",
    "hashtags": ["#maquiagem", "#base"],
}


def test_dica_confere_itens(monkeypatch):
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: dict(RESPOSTA_DICA))
    erros = next(e for e in ESTILOS_DICA if e.chave == "erros")
    t = gerar_dica("maquiagem", erros, [])
    assert [i.titulo for i in t.itens] == ["Pular o hidratante", "Passar demais", "Esquecer o pescoço"]
    assert [i.rotulo for i in t.itens] == ["Erro 1", "Erro 2", "Erro 3"]
    assert t.busca == ["esponja", "hidratante facial"]

    # tema repetido: pede de novo e desiste
    assert gerar_dica("maquiagem", erros, ["3 erros que estragam a sua base"]) is None


def test_dica_mito_ou_verdade(monkeypatch):
    resposta = {**RESPOSTA_DICA, "itens": [
        {"titulo": "Pele oleosa não precisa de hidratante", "texto": "Precisa sim, de textura leve.", "mito_ou_verdade": "mito"},
        {"titulo": "Protetor solar vale em dia nublado", "texto": "Os raios UV passam pelas nuvens.", "mito_ou_verdade": "VERDADE"},
        {"titulo": "Sabonete de rosto pode ser qualquer um", "texto": "O de corpo resseca o rosto.", "mito_ou_verdade": "MITO"},
        {"titulo": "Sem veredito", "texto": "Fica de fora."},
    ]}
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: resposta)
    mito = next(e for e in ESTILOS_DICA if e.chave == "mito")
    t = gerar_dica("skincare", mito, [])
    assert [(i.rotulo, i.icone) for i in t.itens] == [("Mito", "alerta"), ("Verdade", "check"), ("Mito", "alerta")]


def test_tema_do_dia_gira():
    temas = {tema_do_dia(739000 + d) for d in range(12)}
    assert len({a for a, _ in temas}) == 6 and len({e.chave for _, e in temas}) == 4


def test_preparar_dica_com_achadinho(db, monkeypatch):
    db.registrar_postagem("SHOPEE9", "Ofertas", titulo="Esponja de Maquiagem Gota", preco=9.9, link="https://s/9",
                          loja="shopee", preco_original=19.9, imagem_url="https://img/9.jpg")
    monkeypatch.setattr(instagram_legenda, "gemini_configurado", lambda: True)
    monkeypatch.setattr(instagram_legenda, "gerar_json", lambda *a, **k: dict(RESPOSTA_DICA))
    pid = instagram.preparar_post(db, "dica", datetime(2026, 11, 2, 17, 30), agora=AGORA)
    post = db.post_instagram(pid)
    assert post["formato"] == "dica" and post["titulo"] == "3 erros que estragam sua base"
    assert [p["id"] for p in post["produtos"]] == ["SHOPEE9"]
    assert post["n_cards"] == 6  # capa + 3 itens + 1 achadinho + final
    assert "Esponja de Maquiagem Gota: R$ 9,90 (era R$ 19,90, 50% OFF)" in post["legenda"]
    assert "#publi" in post["legenda"]
    assert db.titulos_instagram_recentes("dica") == [post["titulo"]]


# ---------------------------------------------------------------- API da Meta


@pytest.fixture
def api_configurada(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "token-teste")
    monkeypatch.setenv("META_IG_USER_ID", "1784100")
    monkeypatch.setenv("META_MIDIA_URL", "https://bot.exemplo.ts.net:8443/")


def test_aprovado_com_api_publica_e_nao_avisa(db, api_configurada, monkeypatch):
    from publishers import instagram_api

    agora = datetime(2026, 11, 2, 21, 0, tzinfo=instagram._TZ)
    pid = instagram.preparar_do_horario(db, "top5", agora)
    db.atualizar_post_instagram(pid, "aprovado")
    enviados = []
    monkeypatch.setattr(instagram_api, "publicar", lambda urls, legenda: enviados.append((urls, legenda)) or "MID9")

    assert instagram.publicar_do_horario(db, "top5", agora) == []
    post = db.post_instagram(pid)
    assert post["status"] == "publicado" and post["ig_media_id"] == "MID9"
    urls, legenda = enviados[0]
    assert len(urls) == post["n_cards"] and legenda == post["legenda"]
    assert urls[0] == f"https://bot.exemplo.ts.net:8443/ig/{pid}/{instagram_api.assinatura(pid)}/00.jpg"


def test_meta_recusa_vira_falhou_e_avisa_com_o_motivo(db, api_configurada, monkeypatch):
    from publishers import instagram_api

    agora = datetime(2026, 11, 2, 21, 0, tzinfo=instagram._TZ)
    pid = instagram.preparar_do_horario(db, "top5", agora)
    db.atualizar_post_instagram(pid, "aprovado")

    def recusa(urls, legenda):
        raise instagram_api.ErroInstagram("Meta recusou (9004): imagem inacessível")

    monkeypatch.setattr(instagram_api, "publicar", recusa)
    (instrucoes, _), _ = instagram.publicar_do_horario(db, "top5", agora)
    assert db.post_instagram(pid)["status"] == "falhou"
    assert "falhou (Meta recusou (9004): imagem inacessível)" in instrucoes
