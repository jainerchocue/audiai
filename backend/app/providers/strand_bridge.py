"""Adaptador Strands: ejecuta el subagente externo vía otro proceso (Fase 10).

Qué hace este adaptador:

- Implementa el puerto ``RoleExecutor`` con un proceso secundario que ejecuta
  el intérprete de ``strands-agents`` (otro venv, otras credenciales). AudiAI
  nunca ve la clave de OpenRouter: aparece solo en el proceso del subagente.
- El protocolo es JSON por stdin/stdout con tiempo límite; un subagente que
  termine mal, tarde o con salida inválida produce un ``RoleResult`` FAILED
  con motivo explícito, igual que el MockProvider hace con sus scripts.
- No registra secretos: las cabeceras del proceso nunca se escriben en logs
  ni en resultados.

Qué NO hace:

- No concede red: ``allow_network`` True es rechazado explícitamente por la
  entrada del subagente (conexión de la herramienta de búsqueda pendiente y
  sujeta a autorización de costo).
- No cambia las garantías del dominio: las rutas y el intérprete se validan
  al construir el ejecutor, y la respuesta nunca sale cruda: se traduce a los
  objetos del contrato.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.domain.origin import ResultOrigin
from app.domain.roles import (
    FindingProposal,
    RoleError,
    RoleNotConfigured,
    RoleOutcome,
    RoleRequest,
    RoleResult,
)
from app.domain.severity import Severity

_ENTRY_SCRIPT = Path(__file__).resolve().parent / "strands_entry.py"

_SEVERITY_MAP: dict[str, Severity] = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "info": Severity.INFO,
    "unknown": Severity.UNKNOWN,
}

_HYPOTHESIS_DISCLAIMER = (
    "sin evidencia citada: se conserva como hipótesis pendiente de verificación"
)


class StrandsSubagentExecutor:
    """Ejecuta roles vía el subagente Strands de ``agent_root``.

    Requisitos del entorno:
    - ``agent_root/.venv/Scripts/python.exe`` existe (Windows) y tiene
      ``strands-agents`` instalado, con ``OPENROUTER_API_KEY`` y
      ``OPENROUTER_MODEL`` en su ``.env``.
    - La clave jamás transita por AudiAI: el proceso del subagente lee su
      propio ``.env``.
    """

    def __init__(
        self,
        interpreter: Path,
        agent_root: Path,
        *,
        entry_script: Path | None = None,
    ) -> None:
        if not interpreter.is_file():
            raise RoleNotConfigured(
                f"el intérprete del subagente no existe: {interpreter}"
            )
        if not agent_root.is_dir():
            raise RoleNotConfigured(
                f"la raíz del subagente no existe: {agent_root}"
            )
        self._interpreter = interpreter
        self._agent_root = agent_root
        self._entry = entry_script if entry_script is not None else _ENTRY_SCRIPT

    def run(self, request: RoleRequest) -> RoleResult:
        payload = {
            "role": request.role.value,
            "instruction": request.instruction,
            "context": [
                {"evidence_id": item.evidence_id, "snippet": item.snippet}
                for item in request.context
            ],
            "allow_network": request.limits.allow_network,
        }
        timed_out = (
            f"el subagente Strands no respondió en "
            f"{request.limits.timeout_seconds:.1f} s y fue cancelado"
        )
        try:
            process = subprocess.run(  # noqa: S603 — intérprete validado en init
                [str(self._interpreter), str(self._entry)],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                timeout=request.limits.timeout_seconds,
                cwd=str(self._agent_root),
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return self._result(request, {}, error=timed_out)
        except FileNotFoundError:
            return self._result(
                request,
                {},
                error="el intérprete del subagente desapareció durante el run",
            )
        except OSError as error:
            return self._result(
                request,
                {},
                error=f"no se pudo lanzar el proceso del subagente: {error}",
            )

        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()[:400]
            return self._result(
                request,
                {},
                error=(
                    f"el proceso del subagente terminó con código "
                    f"{process.returncode}: {detail}"
                ),
            )
        try:
            answer = json.loads(process.stdout)
        except json.JSONDecodeError:
            head = (process.stdout or "").strip()[:200]
            return self._result(
                request,
                {},
                error=(f"el subagente no respondió JSON válido: {head}"),
            )
        if not isinstance(answer, dict):
            return self._result(
                request,
                {},
                error="la respuesta del subagente no es un objeto",
            )
        return self._result(request, answer)

    # -- construcción del resultado ---------------------------------------

    def _result(
        self,
        request: RoleRequest,
        answer: dict,
        *,
        error: str | None = None,
    ) -> RoleResult:
        model = answer.get("model")
        provider = answer.get("provider")

        if error is not None:
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.FAILED,
                summary="",
                origin=ResultOrigin.MOCK,
                error=error,
            )
        if not _is_text(provider) or not _is_text(model):
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.FAILED,
                summary="",
                origin=ResultOrigin.MOCK,
                error=(
                    "el subagente no identificó proveedor y modelo; "
                    "sin esa identificación el resultado no se registra como real"
                ),
            )

        outcome_message = str(answer.get("outcome", "failed"))
        try:
            outcome = RoleOutcome(outcome_message)
        except ValueError:
            return self._result(
                request,
                {},
                error=f"outcome desconocido del subagente: {outcome_message}",
            )
        if outcome is RoleOutcome.FAILED:
            failed_reason = str(answer.get("error", "motivo no declarado"))
            return self._result(request, {}, error=failed_reason)

        summary = str(answer.get("summary", "")).strip()
        if outcome is RoleOutcome.COMPLETED:
            if not summary:
                return self._result(
                    request,
                    {},
                    error="el subagente completó sin summary",
                )
            proposals = tuple(
                self._proposal_from(item) for item in answer.get("proposals", [])
            )
        else:
            proposals = ()

        return RoleResult(
            role=request.role,
            outcome=outcome,
            summary=summary,
            origin=ResultOrigin.REAL,
            proposals=proposals,
            provider=str(provider),
            model=str(model),
            limitations=(
                "análisis vía subagente externo (Strands/OpenRouter); "
                "el contexto transmitido se comparte con el proveedor",
            ),
        )

    def _proposal_from(self, item: object) -> FindingProposal:
        if not isinstance(item, dict):
            return FindingProposal(
                title="propuesta sin esquema",
                explanation=(
                    "el subagente devolvió un item que no es objeto; "
                    "se registra el fallo de esquema"
                ),
                severity=Severity.UNKNOWN,
                uncertainty=_HYPOTHESIS_DISCLAIMER,
            )
        title = str(item.get("title", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        if not title or not explanation:
            raise RoleError(
                "una propuesta del subagente no tiene título o explicación"
            )
        severity_text = str(item.get("severity", "unknown")).strip().lower()
        evidence_ids = tuple(str(id).strip() for id in item.get("evidence_ids", []))
        uncertainty = item.get("uncertainty")
        return FindingProposal(
            title=title,
            explanation=explanation,
            severity=_SEVERITY_MAP.get(severity_text, Severity.UNKNOWN),
            evidence_ids=evidence_ids,
            uncertainty=(
                str(uncertainty).strip()
                if _is_text(uncertainty)
                else (_HYPOTHESIS_DISCLAIMER if not evidence_ids else None)
            ),
        )


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
