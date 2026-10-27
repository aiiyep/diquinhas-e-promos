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
