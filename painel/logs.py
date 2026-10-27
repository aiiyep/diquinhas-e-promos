from collections import deque
from datetime import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Sao_Paulo")


class MemoriaLogHandler(logging.Handler):
    """Guarda as últimas `capacidade` mensagens de log em memória para exibir no painel."""

    def __init__(self, capacidade: int = 500) -> None:
        super().__init__()
        self._registros: deque[dict[str, Any]] = deque(maxlen=capacidade)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            mensagem = record.getMessage()
            if record.exc_info:
                mensagem += "\n" + logging.Formatter().formatException(record.exc_info)
            self._registros.append(
                {
                    "quando": datetime.fromtimestamp(record.created, _TZ).strftime("%d/%m %H:%M:%S"),
                    "nivel": record.levelname,
                    "origem": record.name.removeprefix("diquinhas_e_promos."),
                    "mensagem": mensagem,
                }
            )
        except Exception:
            self.handleError(record)

    def registros(self) -> list[dict[str, Any]]:
        """Mensagens da mais recente para a mais antiga."""
        return list(reversed(self._registros))


memoria_logs = MemoriaLogHandler()
