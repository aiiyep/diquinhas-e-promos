"""Regressão: a limpeza de cards antigos não pode apagar o post que acabou de ser montado.

Antes, `limpar_cards_antigos()` usava sempre o relógio do sistema. Um post agendado para uma
data mais de 10 dias no passado (como acontece nos testes com data fixa) tinha a pasta apagada
logo depois de criada, e os testes passavam a falhar sozinhos com o passar do calendário.
"""
from datetime import datetime

from orquestrador import instagram


def _pasta(base, quando: datetime):
    p = base / f"{quando:%Y%m%d-%H%M}-top5-abc123"
    p.mkdir(parents=True)
    return p


def test_limpeza_usa_a_data_de_referencia(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram, "PASTA_CARDS", tmp_path)
    ref = datetime(2026, 1, 20, 21, 0)
    recente = _pasta(tmp_path, datetime(2026, 1, 15, 21, 0))
    antiga = _pasta(tmp_path, datetime(2026, 1, 1, 21, 0))

    instagram.limpar_cards_antigos(dias=10, referencia=ref)

    assert recente.exists()
    assert not antiga.exists()


def test_pasta_do_proprio_post_nunca_e_apagada(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram, "PASTA_CARDS", tmp_path)
    post_no_passado = datetime(2020, 5, 1, 11, 30)
    pasta = _pasta(tmp_path, post_no_passado)

    instagram.limpar_cards_antigos(dias=10, referencia=post_no_passado)

    assert pasta.exists()


def test_ignora_pastas_com_nome_fora_do_padrao(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram, "PASTA_CARDS", tmp_path)
    estranha = tmp_path / "rascunhos"
    estranha.mkdir()

    instagram.limpar_cards_antigos(dias=10, referencia=datetime(2026, 1, 20))

    assert estranha.exists()
