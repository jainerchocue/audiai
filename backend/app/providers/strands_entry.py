"""Punto de entrada del subagente Strands (Fase 10): corre FUERA de AudiAI.

Este script lo ejecuta el intérprete del proyecto de subagentes (por ejemplo
``C:\\mi_agente_word\\agente_audi_labs_metaai\\.venv\\Scripts\\python.exe``), el
cual ya tiene instalado ``strands-agents`` y la clave de OpenRouter en su
propio ``.env``. AudiAI nunca lee esa clave: solo pasa JSON por stdin a este
proceso y espera JSON por stdout.

Protocolo (JSON por stdin)
--------------------------
{
  "role": "cause_analysis",            # nombre según app/domain/roles.py
  "instruction": "...",
  "context": [{"evidence_id": "...", "snippet": "..."}],
  "allow_network": false,
  "model_id": null                     # opcional; si falta, usa OPENROUTER_MODEL
}

Respuesta (JSON por stdout)
---------------------------
{
  "outcome": "completed" | "refused" | "failed",
  "summary": "...",
  "provider": "openrouter",
  "model": "<model_id>",
  "proposals": [{"title", "explanation", "severity", "evidence_ids", "uncertainty"}],
  "error": "..."                       # solo con outcome=failed
}

Reglas:
- No incluye ninguna herramienta del proyecto de subagentes: los ``tools``
  originales leen rutas absolutas arbitrarias y eso viola la política de
  lectura acotada de AudiAI. La evidencia la produce únicamente el lector
  acotado del harness.
- ``allow_network=True`` no está conectado: los roles con red exigen
  autorización aparte (y la herramienta de búsqueda original está pagada). Se
  responde FAILED con motivo explícito, nunca se simula.
- Errores de esquema de la respuesta del modelo → FAILED explícito, igual que
  hace el MockProvider: nunca se propagan respuestas crudas.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.prompts import load_agent_prompt, render_agent_prompt

try:
    from dotenv import load_dotenv
    from strands import Agent
    from strands.models.openai import OpenAIModel
except ImportError as error:
    print(
        json.dumps(
            {
                "outcome": "failed",
                "summary": "",
                "provider": "openrouter",
                "model": None,
                "proposals": [],
                "error": (
                    "el entorno del subagente no tiene las dependencias "
                    f"instaladas ({type(error).__name__})"
                ),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0)

MAX_PROPOSALS = 10
SUPPORTED_ROLES = frozenset(
    {
        "cause_analysis",
        "risk_research",
        "web_research",
        "documentation",
        "doc_review",
        "patch_suggestion",
    }
)


def fail(message: str, model_id: str | None) -> dict:
    return {
        "outcome": "failed",
        "summary": "",
        "provider": "openrouter",
        "model": model_id,
        "proposals": [],
        "error": message,
    }


def strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            dropped = "\n".join(lines[1:-1]).strip()
            if dropped:
                return dropped
    return cleaned


def run_agent(payload: dict) -> dict:
    role = str(payload.get("role", "")).strip()
    model_id = payload.get("model_id") or os.getenv("OPENROUTER_MODEL")
    api_key = os.getenv("OPENROUTER_API_KEY")

    if role not in SUPPORTED_ROLES:
        return fail(f"rol no conectado en el subagente: '{role}'", model_id)
    if not model_id:
        return fail(
            "el subagente no tiene OPENROUTER_MODEL configurado en su entorno",
            None,
        )
    if not api_key:
        return fail(
            "el subagente no tiene OPENROUTER_API_KEY configurada en su entorno",
            model_id,
        )
    if payload.get("allow_network"):
        return fail(
            "el modo con red no está conectado; exige autorización aparte",
            model_id,
        )

    context_items = payload.get("context", [])
    valid_ids = [str(item["evidence_id"]) for item in context_items]
    context_lines = [
        f"- [{item['evidence_id']}] {item['snippet']}"
        for item in context_items
    ]
    user_text = render_agent_prompt(
        "strands_user_template",
        instruction=str(payload.get("instruction", "")),
        context="\n".join(context_lines) or "(sin evidencia)",
        evidence_ids=", ".join(valid_ids) or "(ninguno)",
        response_format=load_agent_prompt("strands_output_format"),
    )

    model = OpenAIModel(
        client_args={
            "api_key": api_key,
            "base_url": "https://openrouter.ai/api/v1",
        },
        model_id=model_id,
    )
    system_prompt = "\n\n".join(
        (load_agent_prompt(role), load_agent_prompt("audit_guardrails"))
    )
    agent = Agent(model=model, system_prompt=system_prompt)

    try:
        answer = str(agent(user_text))
    except Exception as error:  # noqa: BLE001 — el fallo se declara, no se oculta
        return fail(
            f"llamada al modelo falló: {type(error).__name__}",
            model_id,
        )

    body = strip_fences(answer)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return fail(
            "el modelo no devolvió JSON válido; primer pasaje: "
            + body[:200],
            model_id,
        )
    if not isinstance(parsed, dict):
        return fail("estructura de respuesta inesperada (no objeto)", model_id)

    if parsed.get("outcome") == "refused":
        return {
            "outcome": "refused",
            "summary": str(parsed.get("reason", "el modelo no puede responder")),
            "provider": "openrouter",
            "model": model_id,
        }

    raw_proposals = parsed.get("proposals", [])
    if not isinstance(raw_proposals, list):
        return fail("el campo 'proposals' no es una lista", model_id)

    proposals = []
    for item in raw_proposals[:MAX_PROPOSALS]:
        if not isinstance(item, dict):
            continue
        evidence_ids = item.get("evidence_ids") or []
        proposals.append(
            {
                "title": str(item.get("title", "")).strip(),
                "explanation": str(item.get("explanation", "")).strip(),
                "severity": str(item.get("severity", "unknown")).strip().lower(),
                "evidence_ids": [str(eid) for eid in evidence_ids],
                "uncertainty": item.get("uncertainty") or None,
            }
        )

    summary = str(parsed.get("summary", "")).strip()
    if not summary and proposals:
        return fail(
            "respuesta del modelo sin 'summary' (debe acompañar a las propuestas)",
            model_id,
        )
    return {
        "outcome": "completed",
        "summary": summary,
        "provider": "openrouter",
        "model": model_id,
        "proposals": proposals,
    }


def main() -> int:
    # El proceso corre con cwd = raíz del subagente: su .env está ahí y jamás
    # se imprime su contenido. Ruta explícita: load_dotenv() sin ruta busca
    # desde el directorio de ESTE módulo, no desde el cwd, y falla en silencio.
    env_path = os.path.join(os.getcwd(), ".env")
    load_dotenv(dotenv_path=env_path if os.path.isfile(env_path) else None)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
    except json.JSONDecodeError as error:
        fail(f"payload de entrada no es JSON válido: {error}", None)
        return 0
    print(json.dumps(run_agent(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
