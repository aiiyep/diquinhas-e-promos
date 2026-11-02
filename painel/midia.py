"""
Servidor só de imagens para a API do Instagram, que baixa cada card de uma URL pública.

Fica numa porta separada do painel (`MIDIA_PORTA`, padrão 8081, em 127.0.0.1) e é publicado na
internet pelo Tailscale Funnel (`tailscale funnel --bg --https=8443 8081`); o painel continua só na
tailnet. Responde apenas `GET /ig/<id>/<assinatura>/<NN>.jpg` de posts que ainda podem ir ao ar, com
a assinatura de `instagram_api.assinatura`. Qualquer outro caminho dá 404.
"""

import hmac
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from database.historico_ofertas import HistoricoDB
from publishers.instagram_api import assinatura

logger = logging.getLogger("diquinhas_e_promos.midia")

_ROTA = re.compile(r"^/ig/(\d{1,9})/([0-9a-f]{32})/(\d{2})\.jpg$")
STATUS_SERVIDOS = ("pendente", "aprovado", "expirado", "falhou")  # os que ainda podem ir ao ar


def arquivo_publico(db: HistoricoDB, caminho: str) -> Optional[Path]:
    """Card pedido pela Meta, ou None se o caminho, a assinatura ou o status não baterem."""
    achado = _ROTA.match(caminho)
    if not achado:
        return None
    post_id, sig, n = int(achado.group(1)), achado.group(2), int(achado.group(3))
    if not hmac.compare_digest(sig, assinatura(post_id)):
        return None
    post = db.post_instagram(post_id)
    if not post or post["status"] not in STATUS_SERVIDOS or not post["pasta"] or n >= post["n_cards"]:
        return None
    arquivo = Path(post["pasta"]) / f"{n:02d}.jpg"
    return arquivo if arquivo.is_file() else None


def iniciar_midia(db: HistoricoDB, host: str = "127.0.0.1", porta: int = 8081) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._servir(com_corpo=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._servir(com_corpo=False)

        def _servir(self, com_corpo: bool) -> None:
            arquivo = arquivo_publico(db, self.path.split("?")[0])
            corpo = arquivo.read_bytes() if arquivo else b"Not found"
            self.send_response(200 if arquivo else 404)
            self.send_header("Content-Type", "image/jpeg" if arquivo else "text/plain")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if com_corpo:
                self.wfile.write(corpo)

        def log_message(self, format: str, *args: Any) -> None:
            return

    servidor = ThreadingHTTPServer((host, porta), Handler)
    servidor.daemon_threads = True
    threading.Thread(target=servidor.serve_forever, name="midia", daemon=True).start()
    logger.info("Servidor de imagens do Instagram em http://%s:%d", host, servidor.server_address[1])
    return servidor
