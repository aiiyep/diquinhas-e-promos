import json
import logging
import urllib.request

from apscheduler.schedulers.background import BackgroundScheduler
import pytest

from database.historico_ofertas import HistoricoDB
from painel import MemoriaLogHandler, iniciar_painel


def _tarefa(nicho: str) -> None:
    pass


@pytest.fixture
def painel(tmp_path):
    db = HistoricoDB(tmp_path / "h.db")
    db.registrar_postagem("MLB1", "Moda", titulo="Vestido <b>Midi</b>", preco=89.9, link="https://x.com", loja="mercado_livre")
    # Postado há 3 dias: fora da fila do WhatsApp, mas ainda tem foto para o painel
    db.registrar_postagem("MLB2", "Moda", titulo="Saia", preco=50.0, link="https://y.com", imagem_url="https://img.example/2.jpg")
    with db._conectar() as conn:
        conn.execute("UPDATE ofertas_enviadas SET data_envio = '2000-01-01T00:00:00' WHERE id_produto = 'MLB2';")
        conn.commit()
    db.registrar_disparo("Moda", True, "Vestido Midi")

    logs = MemoriaLogHandler(capacidade=2)
    logger = logging.getLogger("diquinhas_e_promos.teste_painel")
    logger.addHandler(logs)
    for i in range(3):
        logger.warning("mensagem %d", i)
    logger.removeHandler(logs)

    sched = BackgroundScheduler(timezone="America/Sao_Paulo")
    sched.add_job(_tarefa, trigger="cron", hour=8, minute="30", args=["Moda"], id="j1", name="BLOCO teste")
    sched.start(paused=True)

    servidor = iniciar_painel(sched, db, logs, porta=0)
    yield f"http://127.0.0.1:{servidor.server_address[1]}"
    servidor.shutdown()
    sched.shutdown(wait=False)


def test_api_status(painel):
    with urllib.request.urlopen(f"{painel}/api/status") as resp:
        status = json.loads(resp.read())

    assert status["resumo"]["postagens_hoje"] == 1
    assert status["resumo"]["ultimo_disparo"]["sucesso"] is True
    assert status["proximos"][0]["nicho"] == "Moda"
    assert status["postagens"][0]["titulo"] == "Vestido <b>Midi</b>"
    # buffer guarda só as 2 mais recentes, da mais nova para a mais antiga
    assert [l["mensagem"] for l in status["logs"]] == ["mensagem 2", "mensagem 1"]
    assert status["logs"][0]["origem"] == "teste_painel"


def test_pagina_e_404(painel):
    with urllib.request.urlopen(f"{painel}/") as resp:
        assert "text/html" in resp.headers["Content-Type"]
        assert b"/api/status" in resp.read()
    with pytest.raises(urllib.error.HTTPError) as erro:
        urllib.request.urlopen(f"{painel}/nada")
    assert erro.value.code == 404


def _post(url, corpo, cabecalho=True):
    req = urllib.request.Request(
        url, data=json.dumps(corpo).encode(), method="POST",
        headers={"Content-Type": "application/json", **({"X-Painel": "1"} if cabecalho else {})},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_fila_whatsapp_e_marcar(painel):
    with urllib.request.urlopen(f"{painel}/api/whatsapp") as resp:
        dados = json.loads(resp.read())
    [item] = dados["fila"]
    assert item["id"] == "MLB1" and item["nicho"] == "Moda"
    assert item["texto"].startswith("🔥 *Vestido <b>Midi</b>*")
    assert "R$ 89,90" in item["texto"] and "https://x.com" in item["texto"]

    depois = _post(f"{painel}/api/whatsapp/marcar", {"id": "MLB1", "status": "postado"})
    assert depois == {"fila": [], "postados_hoje": 1}


def test_marcar_whatsapp_rejeita_pedidos_invalidos(painel):
    casos = [
        ({"id": "MLB1", "status": "postado"}, False, 403),  # sem o cabeçalho do painel
        ({"id": "MLB1", "status": "apagado"}, True, 400),
        ({"id": "NAOEXISTE", "status": "postado"}, True, 404),
    ]
    for corpo, cabecalho, codigo in casos:
        with pytest.raises(urllib.error.HTTPError) as erro:
            _post(f"{painel}/api/whatsapp/marcar", corpo, cabecalho)
        assert erro.value.code == codigo


def test_foto_do_produto_passa_pelo_painel(painel, monkeypatch):
    from painel import servidor

    pedidas = []

    def fake_baixar(url):
        pedidas.append(url)
        return b"\x89PNG-fake", "image/png"

    monkeypatch.setattr(servidor, "baixar_foto", fake_baixar)

    with urllib.request.urlopen(f"{painel}/api/whatsapp/foto?id=MLB2") as resp:
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.read() == b"\x89PNG-fake"
    assert pedidas == ["https://img.example/2.jpg"]

    # produto sem foto, inexistente ou sem id: 404 e nada é baixado
    for consulta in ("?id=MLB1", "?id=NAOEXISTE", ""):
        with pytest.raises(urllib.error.HTTPError) as erro:
            urllib.request.urlopen(f"{painel}/api/whatsapp/foto{consulta}")
        assert erro.value.code == 404
    assert len(pedidas) == 1


def test_foto_com_falha_no_download_responde_502(painel, monkeypatch):
    from painel import servidor

    def falha(url):
        raise ValueError("não é imagem")

    monkeypatch.setattr(servidor, "baixar_foto", falha)
    with pytest.raises(urllib.error.HTTPError) as erro:
        urllib.request.urlopen(f"{painel}/api/whatsapp/foto?id=MLB2")
    assert erro.value.code == 502


def test_baixar_foto_recusa_o_que_nao_e_imagem(monkeypatch):
    from painel import servidor

    class _Resp:
        def __init__(self, tipo, corpo):
            self.headers = {"Content-Type": tipo}
            self._corpo = corpo

        def raise_for_status(self):
            pass

        def iter_content(self, _tamanho):
            yield self._corpo

    monkeypatch.setattr(servidor.requests, "get", lambda *a, **k: _Resp("image/jpeg; q=1", b"jpg"))
    assert servidor.baixar_foto("https://x/1.jpg") == (b"jpg", "image/jpeg")

    monkeypatch.setattr(servidor.requests, "get", lambda *a, **k: _Resp("text/html", b"<html>"))
    with pytest.raises(ValueError):
        servidor.baixar_foto("https://x/1.jpg")


@pytest.fixture
def painel_ig(tmp_path):
    from datetime import datetime
    from PIL import Image

    db = HistoricoDB(tmp_path / "h.db")
    pasta = tmp_path / "cards"
    pasta.mkdir()
    for n in range(3):
        Image.new("RGB", (10, 10), "pink").save(pasta / f"{n:02d}.jpg")
    pid = db.criar_post_instagram(
        "top5", datetime(2026, 11, 2, 21, 0), "Top 3 de hoje", "legenda", [{"id": "SHOPEE1", "nome": "Batom", "preco": 9.9}],
        str(pasta), 3, True,
    )
    sched = BackgroundScheduler(timezone="America/Sao_Paulo")
    sched.start(paused=True)
    servidor = iniciar_painel(sched, db, MemoriaLogHandler(), porta=0)
    yield f"http://127.0.0.1:{servidor.server_address[1]}", db, pid
    servidor.shutdown()
    sched.shutdown(wait=False)


def test_instagram_lista_card_e_acoes(painel_ig):
    url, db, pid = painel_ig
    with urllib.request.urlopen(f"{url}/api/instagram") as resp:
        dados = json.loads(resp.read())
    [post] = dados["posts"]
    assert post["id"] == pid and post["status"] == "pendente" and post["formato"] == "Top do dia"
    assert post["produtos"] == [{"nome": "Batom", "preco": 9.9, "desconto": None}]
    assert dados["horarios"] == ["11:30", "17:30", "21:00"]

    with urllib.request.urlopen(f"{url}/api/instagram/card?id={pid}&n=2") as resp:
        assert resp.headers["Content-Type"] == "image/jpeg"
    for consulta in (f"id={pid}&n=3", f"id={pid}&n=-1", "id=999&n=0", "id=x&n=0"):
        with pytest.raises(urllib.error.HTTPError) as erro:
            urllib.request.urlopen(f"{url}/api/instagram/card?{consulta}")
        assert erro.value.code == 404

    depois = _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "aprovar"})
    assert depois["posts"][0]["status"] == "aprovado"

    casos = [
        ({"id": pid, "acao": "aprovar"}, True, 409),  # já aprovado
        ({"id": pid, "acao": "apagar"}, True, 400),
        ({"id": 999, "acao": "aprovar"}, True, 404),
        ({"id": pid, "acao": "descartar"}, False, 403),
    ]
    for corpo, cabecalho, codigo in casos:
        with pytest.raises(urllib.error.HTTPError) as erro:
            _post(f"{url}/api/instagram/acao", corpo, cabecalho)
        assert erro.value.code == codigo

    _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "publicado"})
    assert db.post_instagram(pid)["status"] == "publicado"


def test_instagram_editar_legenda(painel_ig):
    url, db, pid = painel_ig
    nova = "Legenda nova ✨\r\n\r\nContém links de afiliado. #publi  "
    depois = _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "legenda", "legenda": nova})
    assert depois["posts"][0]["legenda"] == "Legenda nova ✨\n\nContém links de afiliado. #publi"
    assert depois["posts"][0]["status"] == "pendente"  # editar não aprova nem desaprova

    # aprovado continua aprovado depois de editar
    _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "aprovar"})
    _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "legenda", "legenda": "Outra\nContém links de afiliado. #publi"})
    assert db.post_instagram(pid)["status"] == "aprovado"
    assert db.post_instagram(pid)["legenda"] == "Outra\nContém links de afiliado. #publi"

    casos = [
        {"legenda": "   "},  # vazia
        {"legenda": "x" * 2201},  # passa do limite do Instagram
        {"legenda": "Contém links de afiliado. #publi " + "#a " * 30},  # mais de 30 hashtags
        {"legenda": "Sem o aviso do CONAR"},  # tirou o #publi que já estava
        {"legenda": "Vou #publicar amanhã"},  # #publicar não é #publi
        {},  # sem legenda
    ]
    for extra in casos:
        with pytest.raises(urllib.error.HTTPError) as erro:
            _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "legenda", **extra})
        assert erro.value.code == 400
    assert db.post_instagram(pid)["legenda"] == "Outra\nContém links de afiliado. #publi"

    # post já publicado não muda mais
    _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "publicado"})
    with pytest.raises(urllib.error.HTTPError) as erro:
        _post(f"{url}/api/instagram/acao", {"id": pid, "acao": "legenda", "legenda": "tarde demais #publi"})
    assert erro.value.code == 409


def test_legenda_sem_aviso_original_pode_ficar_sem_aviso():
    from painel.servidor import validar_legenda

    # só a hashtag já basta, sem a frase "Contém links de afiliado."
    atual = "Texto\n\nContém links de afiliado. #publi"
    assert validar_legenda("Texto curto #publi", atual) == ("Texto curto #publi", "")
    assert validar_legenda("Texto #Publicidade", atual)[0] == "Texto #Publicidade"

    # Dica sem achadinhos não tem link de afiliado, então não leva #publi
    assert validar_legenda("Só a dica", "Dica de skincare") == ("Só a dica", "")
