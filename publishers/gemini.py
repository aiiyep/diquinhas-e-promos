"""
Cliente mínimo da API do Gemini (Google AI Studio), só com `requests`.

Usado para textos curtos (tema da capa e legenda do Instagram). Pede a resposta já em JSON
(`responseSchema`) e, se o modelo principal estiver sobrecarregado ou fora do ar, tenta o
reserva. Chave em `GEMINI_API_KEY`; modelos em `GEMINI_MODELOS` (separados por vírgula).
"""

import json
import logging
import time
from typing import Any, Optional

import requests

from config import env

logger = logging.getLogger("diquinhas_e_promos.gemini")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
# O lite responde em ~1s; o flash é o reserva quando o lite está sobrecarregado
MODELOS_PADRAO = "gemini-3.5-flash-lite,gemini-3.8-flash"
# Respostas que valem uma nova tentativa (limite de uso, sobrecarga, falha momentânea)
_STATUS_TEMPORARIOS = {429, 500, 502, 503, 504}


class GeminiIndisponivel(RuntimeError):
    """Sem chave, sem resposta ou resposta inválida em todos os modelos."""


def gemini_configurado() -> bool:
    return bool(env("GEMINI_API_KEY"))


def _modelos() -> list[str]:
    return [m.strip() for m in (env("GEMINI_MODELOS") or MODELOS_PADRAO).split(",") if m.strip()]


def gerar_json(
    prompt: str,
    schema: dict[str, Any],
    temperatura: float = 0.9,
    tentativas_por_modelo: int = 2,
    timeout: int = 45,
) -> dict[str, Any]:
    """Pede ao Gemini um objeto JSON no formato de `schema` e o devolve já decodificado."""
    chave = env("GEMINI_API_KEY")
    if not chave:
        raise GeminiIndisponivel("GEMINI_API_KEY não configurada")

    corpo = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperatura,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    ultimo_erro: Optional[str] = None
    for modelo in _modelos():
        for tentativa in range(tentativas_por_modelo):
            try:
                resp = requests.post(
                    API_URL.format(modelo=modelo), headers={"x-goog-api-key": chave}, json=corpo, timeout=timeout
                )
            except requests.RequestException as exc:
                ultimo_erro = f"{modelo}: {type(exc).__name__}"
                logger.warning("Gemini %s sem resposta (%s).", modelo, type(exc).__name__)
                continue
            if resp.status_code in _STATUS_TEMPORARIOS:
                ultimo_erro = f"{modelo}: HTTP {resp.status_code}"
                logger.warning("Gemini %s respondeu HTTP %s; tentando de novo.", modelo, resp.status_code)
                time.sleep(2 * (tentativa + 1))
                continue
            if resp.status_code != 200:
                # 400/403/404: modelo inexistente ou chave sem acesso — não adianta repetir
                ultimo_erro = f"{modelo}: HTTP {resp.status_code} {resp.text[:200]}"
                logger.warning("Gemini %s recusou o pedido: HTTP %s", modelo, resp.status_code)
                break
            try:
                partes = resp.json()["candidates"][0]["content"]["parts"]
                texto = "".join(p.get("text", "") for p in partes)
                return json.loads(texto)
            except (KeyError, IndexError, ValueError) as exc:
                ultimo_erro = f"{modelo}: resposta inválida ({exc})"
                logger.warning("Gemini %s devolveu uma resposta fora do formato.", modelo)
    raise GeminiIndisponivel(ultimo_erro or "nenhum modelo configurado")
