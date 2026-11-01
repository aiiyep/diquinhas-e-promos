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
