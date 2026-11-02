from datetime import datetime

import pytest

from database.historico_ofertas import HistoricoDB
from painel import midia
from publishers import instagram_api


class _Resp:
    def __init__(self, dados, ok=True):
        self.dados, self.ok, self.status_code = dados, ok, 200 if ok else 400

    def json(self):
        return self.dados


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "token-teste")
    monkeypatch.setenv("META_IG_USER_ID", "1784100")
    monkeypatch.setenv("META_MIDIA_URL", "https://bot.exemplo.ts.net:8443")


def _falsa_meta(monkeypatch, status="FINISHED"):
    chamadas = []

    def request(metodo, url, data=None, params=None, timeout=None):
        corpo = data if metodo == "POST" else params
        chamadas.append((metodo, url.rsplit("/", 1)[-1] if "/" in url else url, dict(corpo)))
        if metodo == "GET":
            return _Resp({"status_code": status, "status": "Error: imagem inválida"})
        if url.endswith("/media_publish"):
            return _Resp({"id": "MIDIA1"})
        return _Resp({"id": f"C{len(chamadas)}"})

    monkeypatch.setattr(instagram_api.requests, "request", request)
    return chamadas


def test_carrossel_cria_itens_carrossel_e_publica(monkeypatch):
    chamadas = _falsa_meta(monkeypatch)
    assert instagram_api.publicar(["https://x/0.jpg", "https://x/1.jpg"], "legenda", dormir=lambda s: None) == "MIDIA1"
    posts = [c for c in chamadas if c[0] == "POST"]
    assert [p[2].get("image_url") for p in posts[:2]] == ["https://x/0.jpg", "https://x/1.jpg"]
    assert all(p[2]["is_carousel_item"] == "true" for p in posts[:2])
    assert posts[2][2]["media_type"] == "CAROUSEL" and posts[2][2]["children"] == "C1,C2"
    assert posts[2][2]["caption"] == "legenda" and posts[3][1] == "media_publish"
    assert all(c[2]["access_token"] == "token-teste" for c in chamadas)


def test_um_card_vira_foto_simples(monkeypatch):
    chamadas = _falsa_meta(monkeypatch)
    instagram_api.publicar(["https://x/0.jpg"], "oi", dormir=lambda s: None)
    primeiro = chamadas[0][2]
    assert primeiro["image_url"] == "https://x/0.jpg" and primeiro["caption"] == "oi" and "media_type" not in primeiro


def test_container_com_erro_nao_publica(monkeypatch):
    chamadas = _falsa_meta(monkeypatch, status="ERROR")
    with pytest.raises(instagram_api.ErroInstagram, match="imagem inválida"):
        instagram_api.publicar(["https://x/0.jpg"], "oi", dormir=lambda s: None)
    assert not any(c[1] == "media_publish" for c in chamadas)


def test_erro_da_meta_vira_mensagem_legivel(monkeypatch):
    monkeypatch.setattr(instagram_api.requests, "request", lambda *a, **k: _Resp(
        {"error": {"code": 190, "message": "Invalid OAuth access token"}}, ok=False))
    with pytest.raises(instagram_api.ErroInstagram, match=r"\(190\): Invalid OAuth"):
        instagram_api.publicar(["https://x/0.jpg"], "oi", dormir=lambda s: None)


def test_configurado_exige_https(monkeypatch):
    assert instagram_api.configurado()
    monkeypatch.setenv("META_MIDIA_URL", "http://100.1.2.3:8081")
    assert not instagram_api.configurado()


def test_servidor_de_midia_so_entrega_com_assinatura_e_status(tmp_path):
    db = HistoricoDB(tmp_path / "h.db")
    pasta = tmp_path / "post"
    pasta.mkdir()
    (pasta / "00.jpg").write_bytes(b"jpg")
    pid = db.criar_post_instagram(
        "top5", datetime(2026, 11, 2, 21), "t", "l", [], str(pasta), n_cards=1, usou_ia=False,
    )
    url = instagram_api.urls_publicas(pid, 1)[0]
    caminho = url.split("ts.net:8443", 1)[1]
    assert midia.arquivo_publico(db, caminho) == pasta / "00.jpg"
    assert midia.arquivo_publico(db, caminho.replace(instagram_api.assinatura(pid), "0" * 32)) is None
    assert midia.arquivo_publico(db, caminho.replace("00.jpg", "01.jpg")) is None
    assert midia.arquivo_publico(db, "/api/status") is None
    db.atualizar_post_instagram(pid, "publicado")
    assert midia.arquivo_publico(db, caminho) is None
