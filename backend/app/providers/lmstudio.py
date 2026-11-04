"""Ejecutor de roles para el servidor local compatible con OpenAI de LM Studio."""

from __future__ import annotations

import ipaddress
import json
import threading
from collections.abc import Callable
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from app.domain.origin import ResultOrigin
from app.domain.roles import (
    ChatResponse,
    ChatTurn,
    FindingProposal,
    RoleName,
    RoleOutcome,
    RoleRequest,
    RoleResult,
)
from app.prompts import load_agent_prompt, render_agent_prompt
from app.domain.severity import Severity

_MAX_RESPONSE_BYTES = 2_000_000
_MAX_PROPOSALS = 10
_HYPOTHESIS_DISCLAIMER = (
    "sin evidencia citada: se conserva como hipótesis pendiente de verificación"
)
class LMStudioRoleExecutor:
    """Llama una vez por rol a una instancia LM Studio en loopback.

    El modelo se puede fijar por configuración o descubrir en ``/models``.
    No se usa proxy del sistema y la URL no puede apuntar a otro equipo.
    """

    def __init__(
        self,
        base_url: str,
        *,
        model_id: str = "",
        api_key: str = "",
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        self._configured_model = model_id.strip()
        self._api_key = api_key.strip()
        self._model_id: str | None = self._configured_model or None
        self._model_lock = threading.Lock()
        self._opener = build_opener(ProxyHandler({}))

    def run(self, request: RoleRequest) -> RoleResult:
        try:
            model_id = self._resolve_model(request.limits.timeout_seconds)
            payload = self._completion_payload(request, model_id)
            answer = self._request_json(
                self._base_url + "/chat/completions",
                payload,
                request.limits.timeout_seconds,
            )
            content = _completion_content(answer)
            parsed = json.loads(_strip_fences(content))
            return self._result_from_payload(request, model_id, parsed)
        except HTTPError as error:
            detail = {
                400: "revisa que el modelo admita salida JSON estructurada",
                401: "revisa AUDIAI_LMSTUDIO_API_KEY",
                404: "revisa el endpoint y que el modelo esté cargado",
            }.get(error.code, "revisa el estado del servidor local")
            return _failed(
                request,
                f"LM Studio respondió HTTP {error.code}; {detail}",
            )
        except (URLError, TimeoutError, OSError) as error:
            return _failed(
                request,
                f"no se pudo conectar con LM Studio local ({type(error).__name__})",
            )
        except HTTPException as error:
            return _failed(
                request,
                f"respuesta HTTP local inválida ({type(error).__name__})",
            )
        except json.JSONDecodeError as error:
            return _failed(
                request,
                f"respuesta de LM Studio inválida ({type(error).__name__})",
            )
        except ValueError as error:
            return _failed(
                request,
                f"respuesta de LM Studio inválida: {error}",
            )
        except (KeyError, TypeError) as error:
            return _failed(
                request,
                f"respuesta de LM Studio inválida ({type(error).__name__})",
            )

    def _resolve_model(self, timeout: float) -> str:
        with self._model_lock:
            if self._model_id:
                return self._model_id
            response = self._request_json(
                self._base_url + "/models",
                None,
                timeout,
            )
            models = response.get("data")
            if not isinstance(models, list):
                raise ValueError("respuesta /models sin lista de modelos")
            model_ids = [
                item.get("id")
                for item in models
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"].strip()
            ]
            if not model_ids:
                raise ValueError("LM Studio no tiene modelos cargados")
            qwen_model = next(
                (model_id for model_id in model_ids if "qwen" in model_id.casefold()),
                None,
            )
            if qwen_model is None:
                raise ValueError(
                    "no hay un modelo Qwen cargado; carga Qwen o configura "
                    "AUDIAI_LMSTUDIO_MODEL con el identificador de LM Studio"
                )
            self._model_id = qwen_model
            return self._model_id

    def respond(
        self,
        turns: tuple[ChatTurn, ...],
        *,
        timeout_seconds: float,
    ) -> ChatResponse:
        """Responde conversación natural; no inspecciona el proyecto ni usa tools."""

        try:
            model_id = self._resolve_model(timeout_seconds)
            bounded_turns = _bounded_chat_turns(turns)
            payload = self._chat_payload(model_id, bounded_turns, stream=False)
            response = self._request_json(
                self._base_url + "/chat/completions",
                payload,
                timeout_seconds,
            )
            content = _completion_content(response).strip()
            if not content:
                raise ValueError("respuesta conversacional vacía")
            return ChatResponse(
                content=content,
                provider="lmstudio",
                model=model_id,
            )
        except HTTPError as error:
            raise RuntimeError(
                f"LM Studio respondió HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise RuntimeError(
                f"no se pudo conectar con LM Studio local ({type(error).__name__})"
            ) from error
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"respuesta conversacional inválida ({type(error).__name__})"
            ) from error

    def stream_respond(
        self,
        turns: tuple[ChatTurn, ...],
        *,
        timeout_seconds: float,
        on_delta: Callable[[str], None],
    ) -> ChatResponse:
        """Entrega únicamente el texto final, en fragmentos SSE incrementales."""

        try:
            model_id = self._resolve_model(timeout_seconds)
            bounded_turns = _bounded_chat_turns(turns)
            payload = self._chat_payload(model_id, bounded_turns, stream=True)
            content = self._request_stream(
                self._base_url + "/chat/completions",
                payload,
                timeout_seconds,
                on_delta,
            )
            if not content.strip():
                raise ValueError("respuesta conversacional vacía")
            return ChatResponse(
                content=content,
                provider="lmstudio",
                model=model_id,
            )
        except HTTPError as error:
            raise RuntimeError(
                f"LM Studio respondió HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise RuntimeError(
                f"no se pudo conectar con LM Studio local ({type(error).__name__})"
            ) from error
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"respuesta conversacional inválida ({type(error).__name__})"
            ) from error

    @staticmethod
    def _chat_payload(
        model_id: str,
        turns: tuple[ChatTurn, ...],
        *,
        stream: bool,
    ) -> dict:
        return {
            "model": model_id,
            "messages": [
                {
                    "role": "system",
                    "content": load_agent_prompt("chat"),
                },
                *(
                    {"role": turn.role, "content": turn.content}
                    for turn in turns
                ),
            ],
            "temperature": 0.4,
            "max_tokens": 700,
            "reasoning_effort": "none",
            "stream": stream,
        }

    def _request_stream(
        self,
        url: str,
        payload: dict,
        timeout: float,
        on_delta: Callable[[str], None],
    ) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
            },
        )
        if self._api_key:
            request.add_header("Authorization", f"Bearer {self._api_key}")
        content_parts: list[str] = []
        bytes_read = 0
        with self._opener.open(request, timeout=timeout) as response:
            for raw_line in response:
                bytes_read += len(raw_line)
                if bytes_read > _MAX_RESPONSE_BYTES:
                    raise ValueError("respuesta supera el límite local")
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ValueError("choice no es un objeto")
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content_parts.append(text)
                    on_delta(text)
        return "".join(content_parts)

    def _request_json(
        self,
        url: str,
        payload: dict | None,
        timeout: float,
    ) -> dict:
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(url, data=body, headers=headers)
        with self._opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("respuesta supera el límite local")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("respuesta no es un objeto JSON")
        return result

    @staticmethod
    def _completion_payload(request: RoleRequest, model_id: str) -> dict:
        evidence = "\n".join(
            f"- [{item.evidence_id}] {item.snippet}"
            for item in request.context
        )
        user_content = render_agent_prompt(
            "audit_user_template",
            instruction=request.instruction,
            evidence=evidence or "(sin evidencia)",
        )
        return {
            "model": model_id,
            "messages": [
                {
                    "role": "system",
                    "content": "\n\n".join(
                        (
                            load_agent_prompt(request.role.value),
                            load_agent_prompt("audit_guardrails"),
                        )
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "reasoning_effort": "none",
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "audiai_audit_result",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "proposals": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "explanation": {"type": "string"},
                                        "severity": {
                                            "type": "string",
                                            "enum": [
                                                "critical",
                                                "high",
                                                "medium",
                                                "low",
                                                "info",
                                                "unknown",
                                            ],
                                        },
                                        "evidence_ids": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "uncertainty": {
                                            "type": ["string", "null"],
                                        },
                                    },
                                    "required": [
                                        "title",
                                        "explanation",
                                        "severity",
                                        "evidence_ids",
                                        "uncertainty",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["summary", "proposals"],
                        "additionalProperties": False,
                    },
                },
            },
        }

    @staticmethod
    def _result_from_payload(
        request: RoleRequest,
        model_id: str,
        payload: object,
    ) -> RoleResult:
        if not isinstance(payload, dict):
            raise ValueError("respuesta del modelo no es un objeto")
        if payload.get("outcome") == "refused":
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                reason = "el modelo rehusó la solicitud"
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.REFUSED,
                summary=reason.strip(),
                origin=ResultOrigin.REAL,
                provider="lmstudio",
                model=model_id,
                limitations=("modelo ejecutado localmente mediante LM Studio",),
            )

        summary = payload.get("summary")
        proposals = payload.get("proposals")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("falta summary")
        if not isinstance(proposals, list):
            raise ValueError("proposals no es una lista")

        mapped = tuple(
            _proposal_from(item)
            for item in proposals[:_MAX_PROPOSALS]
        )
        return RoleResult(
            role=request.role,
            outcome=RoleOutcome.COMPLETED,
            summary=summary.strip(),
            origin=ResultOrigin.REAL,
            proposals=mapped,
            provider="lmstudio",
            model=model_id,
            limitations=("modelo ejecutado localmente mediante LM Studio",),
        )


def _validated_base_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "LM Studio debe usar una URL HTTP local sin credenciales, query o fragmento"
        )
    host = parts.hostname.lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ValueError("la URL de LM Studio debe apuntar a loopback")
    if parts.path not in ("", "/", "/v1"):
        raise ValueError("la URL base de LM Studio debe terminar en /v1")
    base = value.strip().rstrip("/")
    return base if parts.path == "/v1" else base + "/v1"


def _completion_content(response: dict) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("respuesta sin choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("choice no es un objeto")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("respuesta sin message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("respuesta sin contenido")
    return content


def _bounded_chat_turns(turns: tuple[ChatTurn, ...]) -> tuple[ChatTurn, ...]:
    if not turns or turns[-1].role != "user":
        raise ValueError("la conversación debe terminar en un turno del usuario")
    selected: list[ChatTurn] = []
    remaining = 8_000
    for turn in reversed(turns[-12:]):
        if not isinstance(turn, ChatTurn) or turn.role not in ("user", "assistant"):
            raise ValueError("turno conversacional inválido")
        content = turn.content.strip()
        if not content:
            continue
        content = content[:remaining]
        if not content:
            break
        selected.append(ChatTurn(role=turn.role, content=content))
        remaining -= len(content)
        if remaining <= 0:
            break
    if not selected or selected[0].role != "user":
        raise ValueError("no hay turno de usuario dentro del límite de contexto")
    selected.reverse()
    return tuple(selected)


def _proposal_from(item: object) -> FindingProposal:
    if not isinstance(item, dict):
        raise ValueError("propuesta no es un objeto")
    title = item.get("title")
    explanation = item.get("explanation")
    evidence_ids = item.get("evidence_ids", [])
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(explanation, str)
        or not explanation.strip()
    ):
        raise ValueError("propuesta sin título o explicación")
    if not isinstance(evidence_ids, list) or any(
        not isinstance(evidence_id, str) or not evidence_id.strip()
        for evidence_id in evidence_ids
    ):
        raise ValueError("evidence_ids debe ser una lista de texto")
    severity_name = str(item.get("severity", "unknown")).strip().lower()
    severity = next(
        (value for value in Severity if value.value == severity_name),
        Severity.UNKNOWN,
    )
    uncertainty = item.get("uncertainty")
    if not evidence_ids and not (
        isinstance(uncertainty, str) and uncertainty.strip()
    ):
        uncertainty = _HYPOTHESIS_DISCLAIMER
    elif not isinstance(uncertainty, str):
        uncertainty = None
    return FindingProposal(
        title=title.strip(),
        explanation=explanation.strip(),
        severity=severity,
        evidence_ids=tuple(evidence_ids),
        uncertainty=uncertainty,
    )


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return cleaned


def _failed(request: RoleRequest, reason: str) -> RoleResult:
    return RoleResult(
        role=request.role,
        outcome=RoleOutcome.FAILED,
        error=reason,
    )
