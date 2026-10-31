from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from orquestrador.agendador import BLOCOS, TIMEZONE

ESPERADO = {
    "Rosto e Corpo": [
        "08:30", "08:40", "08:50", "09:00",
        "21:30", "21:36", "21:42", "21:48", "21:54", "22:00",
    ],
    "Unhas": ["11:30", "11:45", "12:00"],
    "Moda": ["13:00", "13:06", "13:12", "13:18", "13:24", "13:30", "18:00", "18:10", "18:20", "18:30"],
    "Cabelo": ["15:30", "15:40", "15:50", "16:00"],
    "K-Beauty": ["19:00", "19:10", "19:20", "19:30"],
    "Beleza Premium": ["20:00", "20:10", "20:20", "20:30"],
    "Ofertas": ["10:00", "10:10", "10:20", "10:30"],
}


def _disparos_do_dia(bloco: dict) -> list[str]:
    tz = ZoneInfo(TIMEZONE)
    trigger = CronTrigger(hour=bloco["hour"], minute=bloco["minute"], timezone=tz)
    inicio = datetime(2026, 1, 15, tzinfo=tz)
    fim = inicio + timedelta(days=1)
    horarios, anterior, atual = [], None, inicio - timedelta(microseconds=1)
    while True:
        atual = trigger.get_next_fire_time(anterior, atual + timedelta(microseconds=1))
        if atual is None or atual >= fim:
            return horarios
        horarios.append(atual.strftime("%H:%M"))
        anterior = atual


def test_blocos_disparam_exatamente_nas_janelas():
    por_nicho: dict[str, list[str]] = {}
    for bloco in BLOCOS:
        por_nicho.setdefault(bloco["nicho"], []).extend(_disparos_do_dia(bloco))
    assert {k: sorted(v) for k, v in por_nicho.items()} == ESPERADO


def test_ids_unicos():
    ids = [b["id"] for b in BLOCOS]
    assert len(ids) == len(set(ids))


def test_intercalar_por_marca_alterna_lojas_e_roda_o_inicio():
    from orquestrador.agendador import _intercalar_por_marca

    cands = [{"id": f"{m}{i}", "marca": m} for m in "ab" for i in (1, 2, 3)] + [{"id": "c1", "marca": "c"}]
    assert [c["id"] for c in _intercalar_por_marca(cands, 0)] == ["a1", "b1", "c1", "a2", "b2", "a3", "b3"]
    assert [c["id"] for c in _intercalar_por_marca(cands, 1)] == ["b1", "c1", "a1", "b2", "a2", "b3", "a3"]
    assert _intercalar_por_marca([], 3) == []


def test_ofertas_alterna_ml_e_shopee_com_fallback(monkeypatch):
    import asyncio
    from orquestrador import agendador

    chamadas = []

    def fonte(nome, resultado):
        async def _f(nicho, idx, controle):
            chamadas.append((nome, idx))
            return resultado[nome]
        return _f

    resultado = {"ml": True, "shopee": True}
    monkeypatch.setattr(agendador, "_disparar_ofertas_ml", fonte("ml", resultado))
    monkeypatch.setattr(agendador, "_disparar_ofertas_shopee", fonte("shopee", resultado))
    controle = agendador._ControleEnvio("Ofertas")

    assert asyncio.run(agendador._disparar_ofertas("Ofertas", 4, controle))
    assert asyncio.run(agendador._disparar_ofertas("Ofertas", 5, controle))
    assert chamadas == [("ml", 2), ("shopee", 2)]

    chamadas.clear()
    resultado["shopee"] = False  # Shopee sem nada inédito: cai para o ML no mesmo disparo
    assert asyncio.run(agendador._disparar_ofertas("Ofertas", 7, controle))
    assert chamadas == [("shopee", 3), ("ml", 3)]


def test_ofertas_shopee_so_com_desconto_real_e_sem_masculino(monkeypatch, tmp_path):
    import asyncio
    from orquestrador import agendador

    (tmp_path / "ofertas.csv").write_text(
        "Item Id,Item Name,Price,Sales,Discount,Offer Link\n"
        '1,Serum Sem Desconto,"50,00",900,,https://s.shopee.com.br/a\n'
        '2,Creme 10 Off,"90,00",100,10%,https://s.shopee.com.br/b\n'
        '3,Perfume Masculino,"60,00",100,50%,https://s.shopee.com.br/c\n'
        '4,Gloss 30 Off,"70,00",5,30%,https://s.shopee.com.br/d\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(agendador.OFERTAS_SHOPEE, "pasta", tmp_path)
    monkeypatch.setattr(agendador.db, "foi_postado_recentemente", lambda _id: False)

    class Controle:
        esgotado = False
        tentados: list = []

        async def tentar(self, produto_id, dados):
            self.tentados.append(produto_id)
            return False

    controle = Controle()
    controle.tentados = []
    monkeypatch.setattr(agendador.ShopeeExtractor, "buscar_imagem_oficial", lambda self, url: None)
    assert not asyncio.run(agendador._disparar_ofertas_shopee("Ofertas", 0, controle))
    assert controle.tentados == ["SHOPEE4", "SHOPEE2"]
