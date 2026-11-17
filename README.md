# 🛍️ Diquinhas & Promos — Affiliate Bot

[![Testes](https://github.com/aiiyep/diquinhas-e-promos/actions/workflows/tests.yml/badge.svg)](https://github.com/aiiyep/diquinhas-e-promos/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-130%2B%20testes-0A9EDC?logo=pytest&logoColor=white)

Bot de afiliados que busca ofertas de beleza e moda no **Mercado Livre** e na **Shopee** e publica cada uma no tópico (thread) correspondente de um supergrupo do **Telegram**, em horários agendados.

---

## 🔁 Como funciona

```text
APScheduler (cron, America/Sao_Paulo)
  └─ disparar_oferta(nicho)                 orquestrador/agendador.py
       ├─ Mercado Livre: catálogo oficial → API de busca pública      extractors/mercadolivre.py
       │   (rotaciona palavras-chave por nicho e pagina 0, 1, 2)
       ├─ Ofertas: mais vendidos de Beleza (API /highlights) com desconto real
       ├─ Shopee: CSVs do painel de afiliados em data/shopee/<marca>/  extractors/shopee.py
       ├─ HistoricoDB: pula produtos postados nos últimos 14 dias       database/historico_ofertas.py
       ├─ PromoFormatter: injeta parâmetros de afiliado no link        formatters/promo_formatter.py
       └─ publish_product_to_telegram: foto + legenda HTML no tópico   publishers/telegram_publisher.py
```

- Preço "De" e nota ⭐ só aparecem quando vêm de dados reais da loja.
- **Tópico Ofertas:**
  - A cada disparo, o bot passa para a próxima subcategoria feminina de Beleza (`OFERTAS_SUBCATEGORIAS`).
  - Dentro dela, publica o mais vendido de maior desconto real que ainda não foi postado.
  - Produtos masculinos são descartados pelo filtro em `extractors/filtros.py`.
  - A página `/ofertas` do ML não é usada porque bloqueia acesso automatizado com um desafio anti-bot.
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
database/                  # SQLite de deduplicação e histórico de disparos
painel/                    # painel web (status, disparos, postagens, log)
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

## 📊 Painel web

Com o bot rodando (`python main.py`), um painel fica disponível em `http://127.0.0.1:8080`. Ele mostra:
- há quanto tempo o bot está no ar;
- o próximo disparo e os seguintes;
- o resultado de cada disparo;
- as últimas postagens;
- o log, com filtro e busca.

O painel só escuta no próprio servidor. Para abrir do seu PC, crie um túnel SSH e acesse `http://localhost:8080`:

```bash
ssh -N -L 8080:localhost:8080 root@IP_DO_DROPLET
```

Porta e ativação podem ser configuradas em `PAINEL_PORTA` e `PAINEL_ATIVO` no `.env`.

## 🧪 Testes

O projeto tem uma suíte com **mais de 130 testes automatizados** em `pytest`, que roda no **GitHub Actions** a cada push e pull request ([`.github/workflows/tests.yml`](./.github/workflows/tests.yml)).

```bash
pip install -r requirements-dev.txt
pytest
```

O que a suíte cobre:

- **Regras de negócio:** agendamento dos blocos de horário, rotação de nichos, filtro de produtos e desconto real
- **Deduplicação:** histórico em SQLite que impede repetir produto dentro da janela de dias
- **Formatação:** links de afiliado e mensagens do Telegram e do Instagram
- **Integrações isoladas com mocks:** Instagram (Graph API), Gemini e as lojas são simulados com `monkeypatch`, então os testes não dependem de rede nem de chave de API
- **Painel web:** rotas e respostas do servidor

> 🐞 **Bug encontrado pelos testes:** a limpeza de cards antigos usava o relógio do sistema, e não a data do post. Com o passar dos dias, os testes com data fixa começaram a falhar sozinhos, porque a pasta do post era apagada logo depois de criada. A correção passou a usar a data do post como referência, com testes de regressão em [`tests/test_limpeza_cards.py`](./tests/test_limpeza_cards.py).

## ➕ Adicionar um nicho

1. Adicione as palavras-chave em `NICHO_ROTATION_QUERIES` e o mapeamento em `NICHO_TO_CATEGORY`.
2. Adicione a linha do horário em `BLOCOS`.
3. Configure `THREAD_ID_<NICHO>` no `.env` e o mapeamento em `_resolve_thread_id`.
