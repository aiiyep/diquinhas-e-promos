from datetime import date, datetime
from io import BytesIO
import json
import urllib.request

from apscheduler.schedulers.background import BackgroundScheduler
from PIL import Image
import pytest

from database.historico_ofertas import HistoricoDB
from orquestrador import reels
from painel import MemoriaLogHandler, iniciar_painel


def _png(cor="pink") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (300, 300), cor).save(buf, "PNG")
    return buf.getvalue()


PRODUTOS = [
    ("MLB1", "Sabonete Líquido Corporal Erva Doce 400ml", 19.9, 29.9),
    ("MLB2", "Hidratante Corporal Nutritivo Cereja 400ml", 24.9, 39.9),
    ("MLB3", "Desodorante Antitranspirante Aerossol Feminino", 12.9, 15.9),
    ("MLB4", "Sabonete Íntimo Suave 200ml", 15.9, None),
    ("MLB5", "Perfume Capilar Floral 100ml", 34.9, 49.9),
    ("MLB6", "Body Splash Morango 200ml", 29.9, 59.9),
    ("MLB7", "Batom Matte Vermelho Longa Duração", 19.9, 29.9),
    ("MLB8", "Lip Tint Cereja Hidratante", 14.9, 24.9),
    ("MLB9", "Protetor Solar Facial FPS 50 Toque Seco", 39.9, 59.9),
    ("MLB10", "Protetor Solar Corporal FPS 30 Spray", 44.9, 69.9),
    ("MLB11", "Shampoo Antifrizz Reparação 300ml", 21.9, 32.9),
    ("MLB12", "Shampoo Hidratação Coco Vegano 250ml", 18.9, 26.9),
    ("MLB13", "Desodorante Masculino Aerossol Sport", 12.9, 19.9),  # fica de fora: masculino
    ("MLB14", "Máscara de Cílios Alongamento Preta", 29.9, 44.9),
    ("MLB15", "Rímel Volume Extremo à Prova d'Água", 25.9, 35.9),
    ("MLB16", "Esmalte Cremoso Nude 8ml", 6.9, 9.9),
    ("MLB17", "Esmalte Cintilante Rosa Glitter", 7.9, 10.9),
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    banco = HistoricoDB(tmp_path / "h.db")
    for pid, titulo, preco, original in PRODUTOS:
        banco.registrar_postagem(pid, "Rosto e Corpo", titulo=titulo, preco=preco, preco_original=original,
                                 link=f"https://ml/{pid}", loja="mercado_livre", imagem_url=f"https://img/{pid}.jpg")
    monkeypatch.setattr(reels, "PASTA_REELS", tmp_path / "reels")
    monkeypatch.setenv("REELS_DIAS", "seg,qua,sex,dom")
    monkeypatch.setattr(reels, "atualizar_precos", lambda itens: list(itens))  # sem rede
    monkeypatch.setattr(reels, "_baixar_foto", lambda url: _png())
    return banco


def _video_falso(slides, destino, fps=30):
    destino.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 2000)
    return destino


def test_datas_especiais():
    assert reels.black_friday(2026) == date(2026, 11, 27)
    assert reels.black_friday(2027) == date(2027, 11, 26)
    assert reels.tema_sazonal(date(2026, 11, 3)).chave == "blackfriday"
    assert reels.tema_sazonal(date(2026, 11, 30)).chave == "blackfriday"  # Cyber Monday
    assert reels.tema_sazonal(date(2026, 12, 1)).chave == "amigosecreto"
    assert reels.tema_sazonal(date(2026, 12, 25)) is None
    assert reels.tema_sazonal(date(2026, 10, 31)) is None


def test_formato_gira_pelos_dias_da_semana(monkeypatch):
    monkeypatch.setenv("REELS_DIAS", "seg,qua,sex,dom")
    semana = [date(2026, 11, d) for d in (2, 4, 6, 8)]  # seg, qua, sex, dom
    assert [reels.formato_do_dia(d) for d in semana] == ["sazonal", "rotina", "duelo", "sazonal"]
    janeiro = [date(2027, 1, d) for d in (4, 6, 8, 10)]
    assert [reels.formato_do_dia(d) for d in janeiro] == ["rotina", "duelo", "rotina", "duelo"]


def test_dias_e_horario_invalidos_voltam_ao_padrao(monkeypatch):
    monkeypatch.setenv("REELS_DIAS", "xyz")
    monkeypatch.setenv("REELS_HORA", "25:00")
    assert reels.dias_configurados() == ["seg", "qua", "sex", "dom"]
    assert reels.horario() == (10, 0)
    monkeypatch.setenv("REELS_DIAS", "Sexta, terça")
    assert reels.dias_configurados() == ["ter", "sex"]


def test_passo_respeita_termos_a_evitar():
    banho = reels.TEMAS_POR_CHAVE["cheirosa"].passos[0]
    assert banho.combina("Sabonete Líquido Corporal 400ml")
    assert not banho.combina("Sabonete Líquido Íntimo 200ml")


def test_rotina_monta_video_legenda_e_nao_repete_produtos(db, monkeypatch):
    monkeypatch.setattr(reels, "montar_video", _video_falso)
    rid = reels.preparar_reel(db, date(2027, 1, 4))  # segunda fora de época: rotina
    reel = db.reel(rid)
    assert reel["formato"] == "rotina" and reel["tema"] == "cheirosa" and reel["status"] == "pendente"
    ids = [p["id"] for p in reel["produtos"]]
    assert "MLB13" not in ids  # masculino
    assert len(ids) == len(set(ids)) >= 3
    assert [p["passo"] for p in reel["produtos"]][:3] == ["Banho", "Pós-banho", "Axila"]
    assert "Contém links de afiliado. #publi" in reel["legenda"] and "R$" not in reel["legenda"]
    assert "1. Banho: Sabonete Líquido Corporal" in reel["legenda"]
    from pathlib import Path
    assert (Path(reel["pasta"]) / "capa.jpg").is_file()
    assert reel["segundos"] == pytest.approx(2.2 + 2.4 * len(ids) + 3.0, abs=0.1)

    # o próximo Reels não usa os mesmos produtos
    segundo = db.reel(reels.preparar_reel(db, date(2027, 1, 4)))
    assert not set(ids) & {p["id"] for p in segundo["produtos"]}


def test_duelo_monta_pares_do_mesmo_tipo(db, monkeypatch):
    monkeypatch.setattr(reels, "montar_video", _video_falso)
    rid = reels.preparar_reel(db, date(2027, 1, 6))  # quarta fora de época: duelo
    reel = db.reel(rid)
    assert reel["formato"] == "duelo"
    passos = [p["passo"] for p in reel["produtos"]]
    assert len(passos) % 2 == 0 and len(passos) >= 4
    assert all(passos[i] == passos[i + 1] for i in range(0, len(passos), 2))
    assert "Rodada 1 ·" in reel["legenda"] and "1)" in reel["legenda"]


def test_sem_produtos_nao_monta(tmp_path, monkeypatch):
    monkeypatch.setattr(reels, "PASTA_REELS", tmp_path / "reels")
    monkeypatch.setenv("REELS_DIAS", "seg,qua,sex,dom")
    db = HistoricoDB(tmp_path / "vazio.db")
    assert reels.preparar_reel(db, date(2027, 1, 6)) is None
    _, aviso, capa = reels.preparar_do_dia(db, datetime(2027, 1, 6, 10, 0, tzinfo=reels._TZ))
    assert "não consegui montar" in aviso and capa is None


def test_video_de_verdade(db):
    pytest.importorskip("imageio_ffmpeg")
    rid = reels.preparar_reel(db, date(2027, 1, 6))
    video = reels.PASTA_REELS.glob("*/video.mp4")
    arquivo = next(video)
    assert arquivo.stat().st_size > 10_000
    assert arquivo.read_bytes()[4:8] == b"ftyp"
    _, aviso, capa = reels.preparar_do_dia(db, datetime(2027, 1, 6, 10, 0, tzinfo=reels._TZ))
    assert "Reels de hoje pronto" in aviso and capa.is_file()
    assert db.reel(rid)["status"] == "pendente"


def test_job_dos_reels_no_agendador(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from orquestrador import agendador

    monkeypatch.setenv("REELS_DIAS", "seg,qua,sex,dom")
    monkeypatch.setenv("REELS_HORA", "10:00")
    sched = agendador.configurar_agendador(AsyncIOScheduler(timezone="America/Sao_Paulo"))
    job = sched.get_job("reels_preparar")
    texto = str(job.trigger)
    assert "day_of_week='mon,wed,fri,sun'" in texto and "hour='10'" in texto and "minute='0'" in texto


# ---------------------------------------------------------------- painel


@pytest.fixture
def painel(db, monkeypatch):
    monkeypatch.setattr(reels, "montar_video", _video_falso)
    rid = reels.preparar_reel(db, date(2027, 1, 4))
    sched = BackgroundScheduler(timezone="America/Sao_Paulo")
    sched.start(paused=True)
    servidor = iniciar_painel(sched, db, MemoriaLogHandler(), porta=0)
    yield f"http://127.0.0.1:{servidor.server_address[1]}", db, rid
    servidor.shutdown()
    sched.shutdown(wait=False)


def _post(url, corpo):
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Painel": "1"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_painel_lista_video_com_range_e_acoes(painel):
    url, db, rid = painel
    with urllib.request.urlopen(f"{url}/api/reels") as resp:
        dados = json.loads(resp.read())
    [reel] = dados["reels"]
    assert reel["id"] == rid and reel["status"] == "pendente" and reel["tem_video"]
    assert reel["tema"] == "Rotina de cheirosa" and reel["formato"] == "Rotina"

    tamanho = len((reels.PASTA_REELS / next(reels.PASTA_REELS.iterdir()).name / "video.mp4").read_bytes())
    req = urllib.request.Request(f"{url}/api/reels/video?id={rid}", headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 206
        assert resp.headers["Content-Range"] == f"bytes 0-99/{tamanho}"
        assert len(resp.read()) == 100
    with urllib.request.urlopen(f"{url}/api/reels/capa?id={rid}") as resp:
        assert resp.headers["Content-Type"] == "image/jpeg"
    for caminho in (f"/api/reels/video?id=999", "/api/reels/capa?id=x"):
        with pytest.raises(urllib.error.HTTPError) as erro:
            urllib.request.urlopen(url + caminho)
        assert erro.value.code == 404

    for corpo, codigo in (({"id": rid, "acao": "apagar"}, 400), ({"id": 999, "acao": "postado"}, 404)):
        with pytest.raises(urllib.error.HTTPError) as erro:
            _post(f"{url}/api/reels/acao", corpo)
        assert erro.value.code == codigo

    depois = _post(f"{url}/api/reels/acao", {"id": rid, "acao": "postado"})
    assert depois["reels"][0]["status"] == "postado"
    with pytest.raises(urllib.error.HTTPError) as erro:
        _post(f"{url}/api/reels/acao", {"id": rid, "acao": "descartar"})
    assert erro.value.code == 409


def test_painel_gerar_outro_troca_os_produtos(painel):
    url, db, rid = painel
    antigos = {p["id"] for p in db.reel(rid)["produtos"]}
    depois = _post(f"{url}/api/reels/acao", {"id": rid, "acao": "regerar"})
    novo = depois["reels"][0]
    assert novo["id"] != rid and db.reel(rid)["status"] == "substituido"
    assert not antigos & {p["id"] for p in db.reel(novo["id"])["produtos"]}


def test_intervalo_range():
    from painel.servidor import _intervalo

    assert _intervalo("bytes=0-99", 1000) == (0, 99)
    assert _intervalo("bytes=900-", 1000) == (900, 999)
    assert _intervalo("bytes=-100", 1000) == (900, 999)
    assert _intervalo("bytes=0-5000", 1000) == (0, 999)
    assert _intervalo("bytes=2000-", 1000) is None
    assert _intervalo("items=0-1", 1000) is None
    assert _intervalo(None, 1000) is None
