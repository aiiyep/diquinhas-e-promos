from datetime import datetime, timedelta
import logging
from pathlib import Path
import sqlite3
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("diquinhas_e_promos.database")

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "historico_ofertas.db"

# Datas gravadas no horário de São Paulo (sem tzinfo), independente do fuso do servidor
_TZ = ZoneInfo("America/Sao_Paulo")

# Colunas de detalhe adicionadas depois da criação original da tabela (migração automática)
_COLUNAS_DETALHE = {
    "titulo": "TEXT",
    "preco": "REAL",
    "link": "TEXT",
    "loja": "TEXT",
    # dados para montar o post do canal do WhatsApp (fila semiautomática do painel)
    "preco_original": "REAL",
    "imagem_url": "TEXT",
    "avaliacao": "REAL",
    "emojis": "TEXT",
    "whatsapp": "TEXT",  # NULL = na fila; "postado" ou "pulado"
    "whatsapp_em": "DATETIME",
}

# Status aceitos para um produto na fila do WhatsApp
STATUS_WHATSAPP = ("postado", "pulado")


def _agora() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


class HistoricoDB:
    """
    Gerenciador SQLite para controle de deduplicação temporal ("memória" do bot).
    Impede que um mesmo `id_produto` seja republicado dentro da janela de `dias_limite` (padrão: 14 dias)
    e guarda o resultado de cada disparo para o painel web.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._criar_tabelas()

    def _conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _criar_tabelas(self) -> None:
        """Cria as tabelas caso ainda não existam e adiciona colunas novas em bancos antigos."""
        with self._conectar() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ofertas_enviadas (
                    id_produto TEXT PRIMARY KEY,
                    nicho TEXT NOT NULL,
                    data_envio DATETIME NOT NULL
                );
                """
            )
            existentes = {row["name"] for row in conn.execute("PRAGMA table_info(ofertas_enviadas);")}
            for coluna, tipo in _COLUNAS_DETALHE.items():
                if coluna not in existentes:
                    conn.execute(f"ALTER TABLE ofertas_enviadas ADD COLUMN {coluna} {tipo};")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS disparos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nicho TEXT NOT NULL,
                    data DATETIME NOT NULL,
                    sucesso INTEGER NOT NULL,
                    detalhe TEXT
                );
                """
            )
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
            row = conn.execute(query, (str(id_produto).strip(),)).fetchone()

        if not row or not row[0]:
            return False

        try:
            data_envio = datetime.fromisoformat(str(row[0]))
        except ValueError:
            try:
                data_envio = datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return False

        limite_corte = _agora() - timedelta(days=dias_limite)
        return data_envio >= limite_corte

    def registrar_postagem(
        self,
        id_produto: str,
        nicho: str,
        titulo: Optional[str] = None,
        preco: Optional[float] = None,
        link: Optional[str] = None,
        loja: Optional[str] = None,
        preco_original: Optional[float] = None,
        imagem_url: Optional[str] = None,
        avaliacao: Optional[float] = None,
        emojis: Optional[str] = None,
    ) -> None:
        """
        Insere um novo registro ou atualiza a `data_envio` (UPSERT) de um produto recém-publicado.
        Um produto repostado volta para a fila do WhatsApp.
        """
        agora_iso = _agora().isoformat(timespec="seconds")
        query = """
        INSERT INTO ofertas_enviadas (
            id_produto, nicho, data_envio, titulo, preco, link, loja,
            preco_original, imagem_url, avaliacao, emojis, whatsapp, whatsapp_em
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(id_produto) DO UPDATE SET
            nicho = excluded.nicho,
            data_envio = excluded.data_envio,
            titulo = excluded.titulo,
            preco = excluded.preco,
            link = excluded.link,
            loja = excluded.loja,
            preco_original = excluded.preco_original,
            imagem_url = excluded.imagem_url,
            avaliacao = excluded.avaliacao,
            emojis = excluded.emojis,
            whatsapp = NULL,
            whatsapp_em = NULL;
        """
        with self._conectar() as conn:
            conn.execute(
                query,
                (
                    str(id_produto).strip(), str(nicho).strip(), agora_iso, titulo, preco, link, loja,
                    preco_original, imagem_url, avaliacao, emojis,
                ),
            )
            conn.commit()

        logger.info(
            "Produto '%s' registrado no histórico (nicho='%s', data_envio=%s).",
            id_produto,
            nicho,
            agora_iso,
        )

    def contar_postagens_nicho(self, nicho: str) -> int:
        """Retorna o total de postagens registradas para um nicho (mantém rotação contínua após reboot)."""
        query = "SELECT COUNT(*) FROM ofertas_enviadas WHERE LOWER(nicho) = LOWER(?);"
        with self._conectar() as conn:
            row = conn.execute(query, (str(nicho).strip(),)).fetchone()
        return int(row[0]) if row and row[0] else 0

    def registrar_disparo(self, nicho: str, sucesso: bool, detalhe: str = "") -> None:
        """Guarda o resultado de um disparo do agendador (publicou ou não, e por quê)."""
        with self._conectar() as conn:
            conn.execute(
                "INSERT INTO disparos (nicho, data, sucesso, detalhe) VALUES (?, ?, ?, ?);",
                (nicho, _agora().isoformat(timespec="seconds"), int(sucesso), detalhe[:500]),
            )
            conn.commit()

    def ultimas_postagens(self, limite: int = 30) -> list[dict[str, Any]]:
        query = """
        SELECT id_produto, nicho, data_envio, titulo, preco, link, loja
        FROM ofertas_enviadas ORDER BY data_envio DESC LIMIT ?;
        """
        with self._conectar() as conn:
            return [dict(row) for row in conn.execute(query, (limite,))]

    def fila_whatsapp(self, horas: int = 48, limite: int = 40) -> list[dict[str, Any]]:
        """
        Postagens das últimas `horas` que ainda não foram postadas nem puladas no canal do
        WhatsApp, do maior desconto para o menor (sem desconto por último) e, no empate, da
        mais recente para a mais antiga.
        """
        desde = (_agora() - timedelta(hours=horas)).isoformat(timespec="seconds")
        query = """
        SELECT id_produto, nicho, data_envio, titulo, preco, preco_original, link, loja,
               imagem_url, avaliacao, emojis
        FROM ofertas_enviadas
        WHERE whatsapp IS NULL AND data_envio >= ? AND link IS NOT NULL AND preco IS NOT NULL
        ORDER BY
            CASE WHEN preco_original > preco THEN 1.0 - preco / preco_original ELSE 0 END DESC,
            data_envio DESC
        LIMIT ?;
        """
        with self._conectar() as conn:
            return [dict(row) for row in conn.execute(query, (desde, limite))]

    def marcar_whatsapp(self, id_produto: str, status: str) -> bool:
        """Marca um produto da fila como `postado` ou `pulado` no canal. True se ele existia."""
        if status not in STATUS_WHATSAPP:
            raise ValueError(f"Status do WhatsApp inválido: {status!r}")
        with self._conectar() as conn:
            cursor = conn.execute(
                "UPDATE ofertas_enviadas SET whatsapp = ?, whatsapp_em = ? WHERE id_produto = ?;",
                (status, _agora().isoformat(timespec="seconds"), str(id_produto).strip()),
            )
            conn.commit()
        return cursor.rowcount > 0

    def ultimos_disparos(self, limite: int = 30) -> list[dict[str, Any]]:
        query = "SELECT nicho, data, sucesso, detalhe FROM disparos ORDER BY id DESC LIMIT ?;"
        with self._conectar() as conn:
            return [{**dict(row), "sucesso": bool(row["sucesso"])} for row in conn.execute(query, (limite,))]

    def resumo_do_dia(self) -> dict[str, Any]:
        """Totais de hoje (horário de São Paulo) para os cartões do painel."""
        hoje = _agora().date().isoformat()
        with self._conectar() as conn:
            postagens = conn.execute(
                "SELECT COUNT(*) FROM ofertas_enviadas WHERE data_envio >= ?;", (hoje,)
            ).fetchone()[0]
            falhas = conn.execute(
                "SELECT COUNT(*) FROM disparos WHERE data >= ? AND sucesso = 0;", (hoje,)
            ).fetchone()[0]
            whatsapp = conn.execute(
                "SELECT COUNT(*) FROM ofertas_enviadas WHERE whatsapp = 'postado' AND whatsapp_em >= ?;", (hoje,)
            ).fetchone()[0]
            ultimo = conn.execute(
                "SELECT nicho, data, sucesso, detalhe FROM disparos ORDER BY id DESC LIMIT 1;"
            ).fetchone()
        return {
            "postagens_hoje": int(postagens),
            "falhas_hoje": int(falhas),
            "whatsapp_hoje": int(whatsapp),
            "ultimo_disparo": {**dict(ultimo), "sucesso": bool(ultimo["sucesso"])} if ultimo else None,
        }
