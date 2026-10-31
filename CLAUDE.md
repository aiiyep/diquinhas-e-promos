# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Bot de afiliados (Python 3, asyncio) que busca ofertas de beleza e moda no Mercado Livre e na Shopee e publica em tópicos de um supergrupo **feminino** do Telegram, em horários fixos. O código, os logs e as mensagens são em português.

## Comandos

```bash
pip install -r requirements-dev.txt           # requirements.txt + pytest (versões fixadas)
pytest                                        # pytest.ini já põe a raiz no pythonpath
pytest tests/test_ofertas.py::test_genero_permitido   # um teste só
python main.py --check-schedule               # lista os jobs agendados e sai (não publica nada)
python main.py                                # roda o agendador + painel web
```

- `python main.py --test-nicho <Nicho>` e `--run-all-now` **publicam de verdade** no grupo do Telegram. Não rode sem o usuário pedir.
- No Windows o venv fica na raiz do checkout principal (`.venv\Scripts\python.exe`). Dentro de uma worktree em `.claude/worktrees/<nome>`, ele está em `..\..\..\.venv`.
- Não há linter configurado.

## Arquitetura

Fluxo: `main.py` → `orquestrador/agendador.py` → extrator → `PromoFormatter.aplicar_afiliado` → `publish_product_to_telegram` → `HistoricoDB`.

- **Agendamento:** a tabela `BLOCOS` em `agendador.py` define cada job cron (fuso `America/Sao_Paulo`). Janelas que atravessam a hora cheia (ex.: 08:30–09:00) viram dois jobs, para não vazar disparos; a docstring do arquivo explica. `tests/test_agendador.py` confere os horários exatos de cada nicho, então atualize o `ESPERADO` ao mexer nos blocos.
- **`disparar_oferta(nicho)`** decide a fonte pela categoria:
  - `beleza_premium` → CSVs da Shopee em `data/shopee/<marca>/` (`ShopeeExtractor`).
  - `k-beauty` → CSVs da Shopee em `data/kbeauty/<loja>/`, alternando a loja a cada disparo (`FONTES_SHOPEE` + `_intercalar_por_marca`). Para atualizar, troque os CSVs exportados da Shopee Afiliados na pasta da loja.
  - `ofertas` → mais vendidos de Beleza pela API oficial do ML (`buscar_ofertas_mais_vendidos`), rodando entre as subcategorias de `OFERTAS_SUBCATEGORIAS`. Em Perfumes usa `buscar_ofertas_por_termos` com `PERFUMES_FEMININOS_TERMOS`, restrito ao domínio `MLB-PERFUMES`.
  - Demais nichos → busca no catálogo do ML rotacionando `NICHO_ROTATION_QUERIES`, páginas 0–2.
- **Envio:** `_ControleEnvio` publica o primeiro produto inédito. Se o envio falhar, tenta o próximo, até `MAX_FALHAS_ENVIO` falhas seguidas. Cada disparo é gravado na tabela `disparos`, que alimenta o painel.
- **`HistoricoDB`** (SQLite em `database/historico_ofertas.db`, fora do git): impede repostar o mesmo `id` por 14 dias. As datas são gravadas no horário de São Paulo, sem fuso. O schema migra sozinho (colunas novas via `ALTER TABLE` em `_criar_tabelas`).
- **`models.ProdutoOferta`**: modelo comum a todas as lojas. `preco_original`, `avaliacao` e `imagem_url` são opcionais, e um "De" menor ou igual ao preço atual é descartado. **Nunca invente preço "De", nota ou imagem**: se a loja não informa, o campo fica `None` e não aparece na legenda.
- **`extractors/filtros.py`**: `eh_produto_masculino` (título, domínio e gênero) e `genero_permitido` (atributo `GENDER` do catálogo: só Feminino/Mulher/Unissex/sem gênero). O grupo é feminino; produtos masculinos e de menino não podem ser postados.
- **`publishers/telegram_publisher.py`**: monta a legenda em HTML, com título e link escapados, e reaproveita um único `Bot`. O tópico é escolhido por `_resolve_thread_id(categoria)` a partir das variáveis `THREAD_ID_*` do `.env`.
- **`config.py`**: carrega o `.env` uma vez. Use `env("NOME")` em vez de `os.getenv`/`load_dotenv` espalhados.
- **Painel web (`painel/`)**: `http.server` da biblioteca padrão, numa thread, iniciado por `main.py`. Mostra `/` (index.html) e `/api/status` (JSON). Escuta em `PAINEL_HOST`:`PAINEL_PORTA`.
- **I/O bloqueante:** as chamadas `requests` rodam via `asyncio.to_thread` dentro das corrotinas do agendador. Não chame HTTP síncrono direto no event loop.

## Restrições da API do Mercado Livre

- Token via `client_credentials` (`ML_CLIENT_ID`/`ML_CLIENT_SECRET`), em cache na classe.
- Endpoints que funcionam: `/products/search`, `/products/{id}`, `/products/{id}/items` (preço e `original_price`) e `/highlights/MLB/category/{id}`.
- `/sites/MLB/search` responde 403 para apps. Destaques do tipo `USER_PRODUCT` (`MLBU…`) também dão 403 e são ignorados.
- A página `mercadolivre.com.br/ofertas` tem desafio anti-bot. Não faça scraping dela nem tente contornar o desafio.
- Use `_api_get` (espera e repete no HTTP 429) para chamadas novas. Destaques e buscas ficam em cache por 6h e dados de produto por 24h; os preços são sempre consultados na hora.

## Produção

- Roda 24/7 num droplet Ubuntu da DigitalOcean via systemd: `bot-promos.service` em `/root/diquinhas-e-promos`, com o venv em `.venv`.
- Deploy: mesclar na `main`, depois no servidor `git pull origin main`, `.venv/bin/pip install -r requirements.txt` e `systemctl restart bot-promos`.
- O SSH do droplet pede senha, então Claude não consegue executar comandos lá. Entregue ao usuário um comando pronto (um `ssh root@… "…"`) e peça a saída.
- Deve existir **um único** processo `main.py`. Um segundo serviço (`diquinhas-bot.service`, com `venv/`) já causou postagens duplicadas e foi desativado. Se houver duplicação, peça primeiro `ps -eo pid,lstart,cmd | grep main.py` no servidor.
- O painel em produção escuta no IP do Tailscale do droplet (`PAINEL_HOST` no `.env` do servidor), acessível pelo Tailscale no celular e no PC.
