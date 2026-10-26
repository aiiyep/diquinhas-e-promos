from datetime import datetime, timedelta
import logging
from pathlib import Path
import sqlite3

logger = logging.getLogger("diquinhas_e_promos.database")

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "historico_ofertas.db"


class HistoricoDB:
    """
    Gerenciador SQLite para controle de deduplicação temporal ("memória" do bot).
    Impede que um mesmo `id_produto` seja republicado dentro da janela de `dias_limite` (padrão: 14 dias).
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._criar_tabela()

    def _conectar(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _criar_tabela(self) -> None:
        """Cria a tabela `ofertas_enviadas` caso ainda não exista."""
        query = """
        CREATE TABLE IF NOT EXISTS ofertas_enviadas (
            id_produto TEXT PRIMARY KEY,
            nicho TEXT NOT NULL,
            data_envio DATETIME NOT NULL
        );
        """
        with self._conectar() as conn:
            conn.execute(query)
            conn.commit()

    def foi_postado_recentemente(self, id_produto: str, dias_limite: int = 14) -> bool:
        """
        Verifica se `id_produto` já foi enviado há menos de `dias_limite` dias.
        Retorna True se ainda estiver no período de bloqueio (quarentena), ou False se estiver liberado.
        """
        if not id_produto:
            return False

        query = "SELECT data_envio FROM ofertas_enviadas WHERE id_produto = ?;"
        with self._conectar() as conn:
            cursor = conn.execute(query, (str(id_produto).strip(),))
            row = cursor.fetchone()

        if not row or not row[0]:
            return False

        try:
            data_envio = datetime.fromisoformat(str(row[0]))
        except ValueError:
            try:
                data_envio = datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return False

        limite_corte = datetime.now() - timedelta(days=dias_limite)
        return data_envio >= limite_corte

    def registrar_postagem(self, id_produto: str, nicho: str) -> None:
        """
        Insere um novo registro ou atualiza a `data_envio` (UPSERT) de um produto recém-publicado.
        """
        agora_iso = datetime.now().isoformat(timespec="seconds")
        query = """
        INSERT INTO ofertas_enviadas (id_produto, nicho, data_envio)
        VALUES (?, ?, ?)
        ON CONFLICT(id_produto) DO UPDATE SET
            nicho = excluded.nicho,
            data_envio = excluded.data_envio;
        """
        with self._conectar() as conn:
            conn.execute(query, (str(id_produto).strip(), str(nicho).strip(), agora_iso))
            conn.commit()

        logger.info(
            "Produto '%s' registrado no histórico (nicho='%s', data_envio=%s).",
            id_produto,
            nicho,
            agora_iso,
        )
