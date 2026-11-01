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
        conn.execute(
            "INSERT INTO ofertas_enviadas (id_produto, nicho, data_envio) VALUES (?, ?, ?)",
            ("MLB2", "Moda", antiga),
        )
    assert not db.foi_postado_recentemente("MLB2")


def test_migra_banco_antigo_sem_colunas_de_detalhe(tmp_path):
    caminho = tmp_path / "antigo.db"
    with sqlite3.connect(caminho) as conn:
        conn.execute(
            "CREATE TABLE ofertas_enviadas (id_produto TEXT PRIMARY KEY, nicho TEXT NOT NULL, data_envio DATETIME NOT NULL)"
        )
        conn.execute("INSERT INTO ofertas_enviadas VALUES ('MLB9', 'Moda', '2026-01-01T10:00:00')")

    db = HistoricoDB(caminho)
    db.registrar_postagem("MLB1", "Cabelo", titulo="Mascara", preco=49.9, link="https://x.com", loja="shopee")

    [nova, antiga] = db.ultimas_postagens()
    assert nova["titulo"] == "Mascara" and nova["preco"] == 49.9
    assert antiga["id_produto"] == "MLB9" and antiga["titulo"] is None


def test_disparos_e_resumo_do_dia(tmp_path):
    db = HistoricoDB(tmp_path / "h.db")
    assert db.resumo_do_dia() == {"postagens_hoje": 0, "falhas_hoje": 0, "whatsapp_hoje": 0, "ultimo_disparo": None}

    db.registrar_postagem("MLB1", "Moda")
    db.registrar_disparo("Moda", True, "Vestido")
    db.registrar_disparo("Unhas", False, "Nenhum produto inédito")

    resumo = db.resumo_do_dia()
    assert resumo["postagens_hoje"] == 1
    assert resumo["falhas_hoje"] == 1
    assert resumo["ultimo_disparo"]["nicho"] == "Unhas"
    assert resumo["ultimo_disparo"]["sucesso"] is False
    assert [d["sucesso"] for d in db.ultimos_disparos()] == [False, True]


def test_fila_whatsapp_ordena_por_desconto_e_some_ao_marcar(tmp_path):
    import pytest

    db = HistoricoDB(tmp_path / "h.db")
    db.registrar_postagem("SEM", "Moda", titulo="Sem desconto", preco=50.0, link="https://x.com/1")
    db.registrar_postagem("D10", "Moda", titulo="10%", preco=90.0, preco_original=100.0, link="https://x.com/2")
    db.registrar_postagem("D50", "Ofertas", titulo="50%", preco=50.0, preco_original=100.0, link="https://x.com/3",
                          imagem_url="https://img/3.jpg", avaliacao=4.8, emojis="🔥")
    db.registrar_postagem("SEMLINK", "Moda")  # registro antigo, sem dados para o post

    assert [p["id_produto"] for p in db.fila_whatsapp()] == ["D50", "D10", "SEM"]
    assert db.fila_whatsapp()[0]["imagem_url"] == "https://img/3.jpg"

    assert db.marcar_whatsapp("D50", "postado")
    assert db.marcar_whatsapp("SEM", "pulado")
    assert not db.marcar_whatsapp("NAOEXISTE", "postado")
    with pytest.raises(ValueError):
        db.marcar_whatsapp("D10", "apagado")
    assert [p["id_produto"] for p in db.fila_whatsapp()] == ["D10"]
    assert db.resumo_do_dia()["whatsapp_hoje"] == 1

    # repostado no Telegram depois da quarentena: volta para a fila
    db.registrar_postagem("D50", "Ofertas", titulo="50%", preco=50.0, preco_original=100.0, link="https://x.com/3")
    assert [p["id_produto"] for p in db.fila_whatsapp()] == ["D50", "D10"]


def test_fila_whatsapp_ignora_postagens_antigas(tmp_path):
    db = HistoricoDB(tmp_path / "h.db")
    antiga = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO ofertas_enviadas (id_produto, nicho, data_envio, preco, link) VALUES (?, ?, ?, ?, ?)",
            ("VELHO", "Moda", antiga, 10.0, "https://x.com"),
        )
    assert db.fila_whatsapp() == []
