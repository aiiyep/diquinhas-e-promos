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
import re
import threading
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from apscheduler.schedulers.base import BaseScheduler
import requests

from database.historico_ofertas import STATUS_WHATSAPP, HistoricoDB
from orquestrador import instagram, reels
from painel.logs import MemoriaLogHandler
from publishers import instagram_api
from publishers.whatsapp import percentual_desconto, montar_texto_whatsapp

logger = logging.getLogger("diquinhas_e_promos.painel")

_TZ = ZoneInfo("America/Sao_Paulo")
_INDEX_HTML = Path(__file__).resolve().parent / "index.html"
_FOTO_MAX_BYTES = 8 * 1024 * 1024


def baixar_foto(url: str) -> tuple[bytes, str]:
    """
    Baixa a foto do produto para o painel repassar ao navegador. As CDNs das lojas não liberam
    CORS, então a página não consegue ler a imagem direto para copiar ou compartilhar.
    """
    resp = requests.get(url, timeout=15, stream=True, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    tipo = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if not tipo.startswith("image/"):
        raise ValueError(f"não é imagem: {tipo!r}")
    corpo = b""
    for pedaco in resp.iter_content(64 * 1024):
        corpo += pedaco
        if len(corpo) > _FOTO_MAX_BYTES:
            raise ValueError("imagem grande demais")
    return corpo, tipo


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
                "nicho": "Instagram" if job.id.startswith(("instagram_", "reels_")) else (job.args[0] if job.args else ""),
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
                "data": str(post["data_envio"] or ""),  # ISO, para ordenar e agrupar por dia
                "titulo": post["titulo"],
                "loja": post["loja"],
                "imagem_url": post["imagem_url"],
                "link": post["link"],
                "desconto": percentual_desconto(post["preco_original"], post["preco"]),
                "texto": montar_texto_whatsapp(post),
            }
        )
    return {
        "fila": fila,
        "postados_hoje": db.resumo_do_dia()["whatsapp_hoje"],
        "hoje": datetime.now(_TZ).date().isoformat(),  # dia em São Paulo, para "Hoje"/"Ontem"
    }


# Ações do painel sobre um carrossel e de quais status cada uma parte
_ACOES_INSTAGRAM = {
    "aprovar": (("pendente", "expirado"), "aprovado"),
    "desaprovar": (("aprovado",), "pendente"),
    "descartar": (("pendente", "aprovado", "expirado", "falhou"), "descartado"),
    "publicado": (("pendente", "aprovado", "expirado", "falhou"), "publicado"),  # publicado à mão
}

# Legenda editada no painel: limites do próprio Instagram
MAX_LEGENDA = 2200
MAX_HASHTAGS = 30
_STATUS_EDITAVEIS = ("pendente", "aprovado", "expirado", "falhou")
# Basta a hashtag (#publi ou #publicidade, como o CONAR aceita); a frase do AVISO_PUBLI é opcional
_TEM_PUBLI = re.compile(r"#publi(cidade)?\b", re.IGNORECASE)


def validar_legenda(nova: Any, atual: Optional[str]) -> tuple[Optional[str], str]:
    """Devolve (legenda pronta, "") ou (None, motivo da recusa)."""
    if not isinstance(nova, str):
        return None, "legenda inválida"
    nova = nova.replace("\r\n", "\n").strip()
    if not nova:
        return None, "a legenda não pode ficar vazia"
    if len(nova) > MAX_LEGENDA:
        return None, f"a legenda tem {len(nova)} caracteres; o Instagram aceita até {MAX_LEGENDA}"
    if nova.count("#") > MAX_HASHTAGS:
        return None, f"o Instagram aceita no máximo {MAX_HASHTAGS} hashtags"
    if _TEM_PUBLI.search(atual or "") and not _TEM_PUBLI.search(nova):
        return None, "mantenha o #publi na legenda (regra do CONAR para links de afiliado)"
    return nova, ""


def montar_instagram(db: HistoricoDB) -> dict[str, Any]:
    posts = []
    for post in db.posts_instagram(9):
        posts.append(
            {
                "id": post["id"],
                "formato": instagram.FORMATOS[formato].nome if (formato := post.get("formato") or post["slot"])
                in instagram.FORMATOS else formato,
                "agendado": _formatar_data_db(post["agendado_para"]),
                "status": post["status"],
                "titulo": post["titulo"],
                "legenda": post["legenda"],
                "n_cards": post["n_cards"],
                "usou_ia": post["usou_ia"],
                "erro": post["erro"],
                "produtos": [
                    {"nome": p.get("nome") or p.get("titulo"), "preco": p.get("preco"), "desconto": p.get("desconto")}
                    for p in post["produtos"]
                ],
            }
        )
    return {
        "posts": posts,
        "horarios": [f"{h:02d}:{m:02d}" for _, h, m in instagram.horarios()],
        "api_conectada": instagram_api.configurado(),
    }


def caminho_card(db: HistoricoDB, post_id: str, indice: str) -> Optional[Path]:
    """Arquivo de um card, só dentro da pasta gravada para o post."""
    try:
        post = db.post_instagram(int(post_id))
        n = int(indice)
    except (TypeError, ValueError):
        return None
    if not post or not post["pasta"] or not 0 <= n < post["n_cards"]:
        return None
    arquivo = Path(post["pasta"]) / f"{n:02d}.jpg"
    return arquivo if arquivo.is_file() else None


def acao_instagram(db: HistoricoDB, post_id: Any, acao: Any, legenda: Any = None) -> tuple[int, str]:
    """Aplica a ação do painel. Devolve (status HTTP, mensagem de erro ou "")."""
    try:
        post = db.post_instagram(int(post_id))
    except (TypeError, ValueError):
        return 400, "id inválido"
    if not post:
        return 404, "carrossel não encontrado"
    if acao == "regerar":
        if instagram.regerar_post(db, post["id"]) is None:
            return 409, "não deu para montar outro (faltam produtos inéditos?)"
        return 200, ""
    if acao == "legenda":
        if post["status"] not in _STATUS_EDITAVEIS:
            return 409, f"carrossel está '{post['status']}'"
        nova, erro = validar_legenda(legenda, post["legenda"])
        if nova is None:
            return 400, erro
        db.atualizar_legenda_instagram(post["id"], nova)
        return 200, ""
    if acao not in _ACOES_INSTAGRAM:
        return 400, "ação inválida"
    origens, destino = _ACOES_INSTAGRAM[acao]
    if post["status"] not in origens:
        return 409, f"carrossel está '{post['status']}'"
    db.atualizar_post_instagram(post["id"], destino)
    return 200, ""


def montar_reels(db: HistoricoDB) -> dict[str, Any]:
    lista = []
    for r in db.reels(6):
        tema = reels.TEMAS_POR_CHAVE.get(r["tema"])
        lista.append(
            {
                "id": r["id"],
                "dia": datetime.fromisoformat(r["dia"]).strftime("%d/%m"),
                "status": r["status"],
                "formato": reels.NOMES_FORMATOS.get(r["formato"], r["formato"]),
                "tema": tema.rotulo if tema else r["tema"],
                "titulo": r["titulo"],
                "legenda": r["legenda"],
                "segundos": r["segundos"],
                "produtos": [p.get("nome") for p in r["produtos"]],
                "tem_video": bool(caminho_reel(db, r["id"], "video.mp4")),
            }
        )
    hora, minuto = reels.horario()
    return {
        "reels": lista,
        "agenda": f"{', '.join(reels.dias_configurados())} às {hora:02d}:{minuto:02d}",
        "gerando": reels.gerando(),
    }


def caminho_reel(db: HistoricoDB, reel_id: Any, nome: str) -> Optional[Path]:
    """`video.mp4` ou `capa.jpg` de um Reels, só dentro da pasta gravada para ele."""
    if nome not in ("video.mp4", "capa.jpg"):
        return None
    try:
        reel = db.reel(int(reel_id))
    except (TypeError, ValueError):
        return None
    if not reel or not reel["pasta"]:
        return None
    arquivo = Path(reel["pasta"]) / nome
    return arquivo if arquivo.is_file() else None


def acao_reels(db: HistoricoDB, reel_id: Any, acao: Any) -> tuple[int, str]:
    """postado / descartar / regerar um Reels, ou "gerar" um novo para hoje."""
    if acao == "gerar":
        if reels.gerando():
            return 409, "já estou montando um Reels, espere terminar"
        return (200, "") if reels.preparar_reel(db) else (409, "não deu para montar (faltam produtos com foto?)")
    try:
        reel = db.reel(int(reel_id))
    except (TypeError, ValueError):
        return 400, "id inválido"
    if not reel:
        return 404, "Reels não encontrado"
    if acao == "regerar":
        if reels.gerando():
            return 409, "já estou montando um Reels, espere terminar"
        if reel["status"] not in ("pendente", "descartado"):
            return 409, f"Reels está '{reel['status']}'"
        return (200, "") if reels.regerar(db, reel["id"]) else (409, "não deu para montar outro (faltam produtos?)")
    destinos = {"postado": "postado", "descartar": "descartado"}
    if acao not in destinos:
        return 400, "ação inválida"
    if reel["status"] != "pendente":
        return 409, f"Reels está '{reel['status']}'"
    db.atualizar_reel(reel["id"], destinos[acao])
    return 200, ""


def _intervalo(cabecalho: Optional[str], tamanho: int) -> Optional[tuple[int, int]]:
    """Primeiro intervalo de um cabeçalho `Range: bytes=a-b` (o Safari só toca vídeo com Range)."""
    if not cabecalho or not cabecalho.startswith("bytes="):
        return None
    inicio_txt, _, fim_txt = cabecalho[6:].split(",")[0].strip().partition("-")
    try:
        if inicio_txt:
            inicio = int(inicio_txt)
            fim = min(int(fim_txt), tamanho - 1) if fim_txt else tamanho - 1
        else:  # "bytes=-500": os últimos 500 bytes
            inicio, fim = max(0, tamanho - int(fim_txt)), tamanho - 1
    except ValueError:
        return None
    return (inicio, fim) if 0 <= inicio <= fim < tamanho else None


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
            elif caminho == "/api/whatsapp/foto":
                self._foto()
            elif caminho == "/api/instagram":
                self._json(lambda: montar_instagram(db))
            elif caminho == "/api/instagram/card":
                consulta = parse_qs(urlsplit(self.path).query)
                arquivo = caminho_card(db, (consulta.get("id") or [""])[0], (consulta.get("n") or [""])[0])
                if arquivo is None:
                    self._responder(404, "text/plain; charset=utf-8", "Card não encontrado".encode("utf-8"))
                else:
                    self._responder(200, "image/jpeg", arquivo.read_bytes(), cache="private, max-age=600")
            elif caminho == "/api/reels":
                self._json(lambda: montar_reels(db))
            elif caminho in ("/api/reels/video", "/api/reels/capa"):
                nome = "video.mp4" if caminho.endswith("video") else "capa.jpg"
                arquivo = caminho_reel(db, (parse_qs(urlsplit(self.path).query).get("id") or [""])[0], nome)
                if arquivo is None:
                    self._responder(404, "text/plain; charset=utf-8", "Arquivo não encontrado".encode("utf-8"))
                elif nome == "capa.jpg":
                    self._responder(200, "image/jpeg", arquivo.read_bytes(), cache="private, max-age=600")
                else:
                    self._video(arquivo)
            else:
                self._responder(404, "text/plain; charset=utf-8", "Não encontrado".encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            caminho = self.path.split("?")[0]
            if caminho not in ("/api/whatsapp/marcar", "/api/instagram/acao", "/api/reels/acao"):
                self._responder(404, "text/plain; charset=utf-8", "Não encontrado".encode("utf-8"))
                return
            # Cabeçalho próprio: um formulário de outro site não consegue enviá-lo sem CORS
            if self.headers.get("X-Painel") != "1":
                self._responder(403, "application/json; charset=utf-8", b'{"erro": "proibido"}')
                return
            try:
                tamanho = min(int(self.headers.get("Content-Length") or 0), 64_000)  # legenda de até 2.200 caracteres com emojis
                dados = json.loads(self.rfile.read(tamanho) or b"{}")
                if not isinstance(dados, dict):
                    raise ValueError
            except ValueError:
                dados = {}
            if caminho == "/api/instagram/acao":
                codigo, erro = acao_instagram(db, dados.get("id"), dados.get("acao"), dados.get("legenda"))
                if codigo != 200:
                    corpo = json.dumps({"erro": erro}, ensure_ascii=False).encode("utf-8")
                    self._responder(codigo, "application/json; charset=utf-8", corpo)
                else:
                    self._json(lambda: montar_instagram(db))
                return
            if caminho == "/api/reels/acao":
                codigo, erro = acao_reels(db, dados.get("id"), dados.get("acao"))
                if codigo != 200:
                    corpo = json.dumps({"erro": erro}, ensure_ascii=False).encode("utf-8")
                    self._responder(codigo, "application/json; charset=utf-8", corpo)
                else:
                    self._json(lambda: montar_reels(db))
                return
            id_produto, status = str(dados.get("id") or ""), dados.get("status")
            if not id_produto or status not in STATUS_WHATSAPP:
                self._responder(400, "application/json; charset=utf-8", b'{"erro": "id ou status invalido"}')
                return
            if not db.marcar_whatsapp(id_produto, status):
                self._responder(404, "application/json; charset=utf-8", b'{"erro": "produto nao encontrado"}')
                return
            self._json(lambda: montar_fila_whatsapp(db))

        def _foto(self) -> None:
            # Só baixa a foto já gravada para o produto: o painel não vira proxy de URL qualquer
            id_produto = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
            url = db.imagem_do_produto(id_produto) if id_produto else None
            if not url or not url.startswith(("http://", "https://")):
                self._responder(404, "text/plain; charset=utf-8", "Sem foto".encode("utf-8"))
                return
            try:
                corpo, tipo = baixar_foto(url)
            except Exception as exc:
                logger.warning("Falha ao baixar a foto de %s: %s", id_produto, exc)
                self._responder(502, "text/plain; charset=utf-8", "Falha ao baixar a foto".encode("utf-8"))
                return
            self._responder(200, tipo, corpo, cache="private, max-age=3600")

        def _video(self, arquivo: Path) -> None:
            tamanho = arquivo.stat().st_size
            intervalo = _intervalo(self.headers.get("Range"), tamanho)
            inicio, fim = intervalo or (0, tamanho - 1)
            with arquivo.open("rb") as f:
                f.seek(inicio)
                corpo = f.read(fim - inicio + 1)
            self.send_response(206 if intervalo else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            if intervalo:
                self.send_header("Content-Range", f"bytes {inicio}-{fim}/{tamanho}")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "private, max-age=600")
            self.end_headers()
            self.wfile.write(corpo)

        def _json(self, montar: Any) -> None:
            try:
                corpo = json.dumps(montar(), ensure_ascii=False)
                self._responder(200, "application/json; charset=utf-8", corpo.encode("utf-8"))
            except Exception as exc:
                logger.error("Erro ao montar resposta do painel: %s", exc)
                self._responder(500, "application/json; charset=utf-8", b'{"erro": "falha interna"}')

        def _responder(self, status: int, tipo: str, corpo: bytes, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", cache)
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
