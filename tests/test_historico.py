from datetime import datetime, timedelta
import sqlite3

from database.historico_ofertas import HistoricoDB


def test_registrar_e_consultar(tmp_path):
    db = HistoricoDB(tmp_path / "h.db")
    assert not db.foi_postado_recentemente("MLB1")
    db.registrar_postagem("MLB1", "Moda")
    assert db.foi_postado_recentemente("MLB1")
    assert db.contar_postagens_nicho("moda") == 1


def test_postagem_antiga_liberada(tmp_path):
    db = HistoricoDB(tmp_path / "h.db")
    antiga = (datetime.now() - timedelta(days=15)).isoformat(timespec="seconds")
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("INSERT INTO ofertas_enviadas VALUES (?, ?, ?)", ("MLB2", "Moda", antiga))
    assert not db.foi_postado_recentemente("MLB2")
