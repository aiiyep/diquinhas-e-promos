"""
Painel web do bot: uma página que mostra status, próximos disparos, postagens e logs.

Usa só a biblioteca padrão (`http.server`) numa thread separada, para não pesar no droplet.
Por padrão escuta em 127.0.0.1 — acesse do seu PC por túnel SSH:
    ssh -L 8080:localhost:8080 root@IP_DO_DROPLET
e abra http://localhost:8080 no navegador.
"""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import threading
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.base import BaseScheduler

from database.historico_ofertas import STATUS_WHATSAPP, HistoricoDB
from painel.logs import MemoriaLogHandler
from publishers.whatsapp import percentual_desconto, montar_texto_whatsapp

logger = logging.getLogger("diquinhas_e_promos.painel")

_TZ = ZoneInfo("America/Sao_Paulo")
_INDEX_HTML = Path(__file__).resolve().parent / "index.html"


def _formatar_data_db(valor: Optional[str]) -> str:
    """Datas do SQLite já estão no horário de São Paulo (ISO sem fuso)."""
    if not valor:
        return ""
    try:
        return datetime.fromisoformat(str(valor)).strftime("%d/%m %H:%M")
    except ValueError:
        return str(valor)


def montar_status(
    scheduler: BaseScheduler,
    db: HistoricoDB,
    logs: MemoriaLogHandler,
    iniciado_em: datetime,
) -> dict[str, Any]:
    """Junta tudo o que a página mostra num único JSON."""
    agora = datetime.now(_TZ)

    proximos = []
    for job in scheduler.get_jobs():
        quando = getattr(job, "next_run_time", None)
        if quando is None:
            continue
        quando = quando.astimezone(_TZ)
        proximos.append(
            {
                "nicho": job.args[0] if job.args else "",
                "nome": job.name,
                "quando": quando.strftime("%d/%m %H:%M"),
                "em_segundos": max(0, int((quando - agora).total_seconds())),
            }
        )
    proximos.sort(key=lambda p: p["em_segundos"])

    resumo = db.resumo_do_dia()
    if resumo["ultimo_disparo"]:
        resumo["ultimo_disparo"]["data"] = _formatar_data_db(resumo["ultimo_disparo"]["data"])

    postagens = db.ultimas_postagens(30)
    for postagem in postagens:
        postagem["data_envio"] = _formatar_data_db(postagem["data_envio"])

    disparos = db.ultimos_disparos(30)
    for disparo in disparos:
        disparo["data"] = _formatar_data_db(disparo["data"])

    return {
        "agora": agora.strftime("%d/%m/%Y %H:%M:%S"),
        "no_ar_segundos": int((agora - iniciado_em).total_seconds()),
        "resumo": resumo,
        "proximos": proximos,
        "postagens": postagens,
        "disparos": disparos,
        "logs": logs.registros(),
    }


def montar_fila_whatsapp(db: HistoricoDB) -> dict[str, Any]:
    """Postagens recentes ainda não levadas ao canal do WhatsApp, com o texto pronto para colar."""
    fila = []
    for post in db.fila_whatsapp():
        fila.append(
            {
                "id": post["id_produto"],
                "nicho": post["nicho"],
                "quando": _formatar_data_db(post["data_envio"]),
                "titulo": post["titulo"],
                "loja": post["loja"],
                "imagem_url": post["imagem_url"],
                "link": post["link"],
                "desconto": percentual_desconto(post["preco_original"], post["preco"]),
                "texto": montar_texto_whatsapp(post),
            }
        )
    return {"fila": fila, "postados_hoje": db.resumo_do_dia()["whatsapp_hoje"]}


def iniciar_painel(
    scheduler: BaseScheduler,
    db: HistoricoDB,
    logs: MemoriaLogHandler,
    host: str = "127.0.0.1",
    porta: int = 8080,
) -> ThreadingHTTPServer:
    """Sobe o servidor HTTP do painel numa thread daemon e o devolve (use `.shutdown()` para parar)."""
    iniciado_em = datetime.now(_TZ)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (nome exigido pelo http.server)
            caminho = self.path.split("?")[0]
            if caminho == "/":
                self._responder(200, "text/html; charset=utf-8", _INDEX_HTML.read_bytes())
            elif caminho == "/api/status":
                self._json(lambda: montar_status(scheduler, db, logs, iniciado_em))
            elif caminho == "/api/whatsapp":
                self._json(lambda: montar_fila_whatsapp(db))
            else:
                self._responder(404, "text/plain; charset=utf-8", "Não encontrado".encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            caminho = self.path.split("?")[0]
            if caminho != "/api/whatsapp/marcar":
                self._responder(404, "text/plain; charset=utf-8", "Não encontrado".encode("utf-8"))
                return
            # Cabeçalho próprio: um formulário de outro site não consegue enviá-lo sem CORS
            if self.headers.get("X-Painel") != "1":
                self._responder(403, "application/json; charset=utf-8", b'{"erro": "proibido"}')
                return
            try:
                tamanho = min(int(self.headers.get("Content-Length") or 0), 10_000)
                dados = json.loads(self.rfile.read(tamanho) or b"{}")
                id_produto, status = str(dados.get("id") or ""), dados.get("status")
            except (ValueError, AttributeError):
                id_produto, status = "", None
            if not id_produto or status not in STATUS_WHATSAPP:
                self._responder(400, "application/json; charset=utf-8", b'{"erro": "id ou status invalido"}')
                return
            if not db.marcar_whatsapp(id_produto, status):
                self._responder(404, "application/json; charset=utf-8", b'{"erro": "produto nao encontrado"}')
                return
            self._json(lambda: montar_fila_whatsapp(db))

        def _json(self, montar: Any) -> None:
            try:
                corpo = json.dumps(montar(), ensure_ascii=False)
                self._responder(200, "application/json; charset=utf-8", corpo.encode("utf-8"))
            except Exception as exc:
                logger.error("Erro ao montar resposta do painel: %s", exc)
                self._responder(500, "application/json; charset=utf-8", b'{"erro": "falha interna"}')

        def _responder(self, status: int, tipo: str, corpo: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, format: str, *args: Any) -> None:
            # Silencia o log de cada requisição (a página atualiza a cada poucos segundos)
            return

    servidor = ThreadingHTTPServer((host, porta), Handler)
    servidor.daemon_threads = True
    threading.Thread(target=servidor.serve_forever, name="painel", daemon=True).start()
    logger.info("Painel web disponível em http://%s:%d", host, servidor.server_address[1])
    return servidor
