# 🛍️ Diquinhas & Promos — Affiliate Bot

Bot de afiliados que busca ofertas de beleza e moda no **Mercado Livre** e na **Shopee** e publica cada uma no tópico (thread) correspondente de um supergrupo do **Telegram**, em horários agendados.

---

## 🔁 Como funciona

```text
APScheduler (cron, America/Sao_Paulo)
  └─ disparar_oferta(nicho)                 orquestrador/agendador.py
       ├─ Mercado Livre: catálogo oficial → API de busca pública      extractors/mercadolivre.py
       │   (rotaciona palavras-chave por nicho e pagina 0, 1, 2)
       ├─ Shopee: CSVs do painel de afiliados em data/shopee/<marca>/  extractors/shopee.py
       ├─ HistoricoDB: pula produtos postados nos últimos 14 dias       database/historico_ofertas.py
       ├─ PromoFormatter: injeta parâmetros de afiliado no link        formatters/promo_formatter.py
       └─ publish_product_to_telegram: foto + legenda HTML no tópico   publishers/telegram_publisher.py
```

- Preço "De" e nota ⭐ só aparecem quando vêm de dados reais da loja.
- Se um envio falha, o bot tenta o próximo produto (até 3 falhas seguidas).
- Os horários dos blocos ficam na tabela `BLOCOS` em `orquestrador/agendador.py`.

## 📂 Estrutura

```text
config.py                  # carrega o .env e define constantes compartilhadas
models.py                  # ProdutoOferta (modelo Pydantic comum a todas as lojas)
main.py                    # CLI
orquestrador/agendador.py  # blocos de horário e lógica de disparo
extractors/                # mercadolivre.py, shopee.py
formatters/                # links de afiliado
publishers/                # Telegram
database/                  # SQLite de deduplicação
data/shopee/<marca>/*.csv  # exportações da Shopee Afiliados
tests/                     # pytest
```

## ▶️ Como rodar

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # preencha token, chat_id, thread_ids e credenciais do ML
```

| Comando | O que faz |
| --- | --- |
| `python main.py` | Inicia o agendador e fica rodando |
| `python main.py --check-schedule` | Lista os próximos disparos e encerra |
| `python main.py --test-nicho Moda` | Faz um disparo imediato de um nicho |
| `python main.py --run-all-now` | Publica um lote (Shopee + containers ML) agora |

## 🧪 Testes

```bash
pip install -r requirements-dev.txt
pytest
```

## ➕ Adicionar um nicho

1. Adicione as palavras-chave em `NICHO_ROTATION_QUERIES` e o mapeamento em `NICHO_TO_CATEGORY`.
2. Adicione a linha do horário em `BLOCOS`.
3. Configure `THREAD_ID_<NICHO>` no `.env` e o mapeamento em `_resolve_thread_id`.
