# 🛍️ Diquinhas & Promos — Affiliate Bot

Arquitetura base modular e desacoplada para extração, formatação e publicação automatizada de ofertas (Moda, Beleza e Tecnologia) integrando **Amazon**, **Shopee** e **Mercado Livre** com disparo para **Telegram (Tópicos/Threads)** e **Twitter/X**.

---

## 📂 Estrutura do Projeto

```text
diquinhas-e-promos/
├── extractors/              # Conectores isolados por loja + contratos Pydantic
│   ├── __init__.py
│   ├── base.py              # BaseExtractor (ABC), ProductOffer, Category, Marketplace
│   ├── amazon.py            # AmazonExtractor (PA-API 5.0)
│   ├── shopee.py            # ShopeeExtractor (GraphQL Affiliate API)
│   └── mercado_livre.py     # MercadoLivreExtractor (MLB REST API)
├── formatters/              # Montagem de copy, comparativo de preços e tracking links
│   ├── __init__.py
│   └── promo_formatter.py   # PromoFormatter e FormattedPromotion
├── publishers/              # Clientes de publicação para redes sociais/mensageiros
│   ├── __init__.py
│   ├── base.py              # BasePublisher (ABC)
│   ├── telegram_publisher.py# Suporte a message_thread_id (Tópicos)
│   └── twitter_publisher.py # Integração Tweepy API v2
├── .env.example             # Mapeamento de variáveis de ambiente e credenciais
├── main.py                  # Orquestrador central (Extrai -> Formata -> Publica)
├── requirements.txt         # Dependências do projeto
└── README.md
```

---

## 🐧 Como Rodar no Fedora Linux

### 1. Instalar pacotes base do Python (caso não estejam instalados)
```bash
sudo dnf update -y
sudo dnf install -y python3 python3-pip python3-virtualenv
```

### 2. Acessar o diretório do projeto
```bash
cd diquinhas-e-promos
```

### 3. Criar e ativar o ambiente virtual (`venv`)
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Atualizar o `pip` e instalar as dependências
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Configurar as variáveis de ambiente
Copie o arquivo de exemplo `.env.example` para `.env` e preencha suas chaves de API:
```bash
cp .env.example .env
nano .env
```

### 6. Executar o orquestrador principal
```bash
python3 main.py
```

---

## 🔌 Como Adicionar ou Remover Lojas
Graças ao contrato `BaseExtractor` em `extractors/base.py`:
- **Remover uma loja**: Basta comentar ou remover a instância correspondente na lista `extractors` dentro de `build_orchestrator()` em `main.py`.
- **Adicionar uma nova loja (ex.: Magalu, AliExpress)**: Crie uma nova subclasse de `BaseExtractor` implementando `fetch_offers()` e `_normalize_payload()` retornando objetos `ProductOffer`, e registre-a na lista `extractors` do `main.py`.
