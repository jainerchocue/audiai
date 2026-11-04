"""Fase 10: adaptador del subagente Strands sin llamadas reales.

Ninguna prueba de este archivo consulta la red: el "subagente" es un script
stub que el intérprete de pytest ejecuta, emulando los casos posibles del
protocolo stdin→stdout definido por ``strands_entry.py``. La prueba REAL de
extremo a extremo con el proveedor se hace aparte, con autorización.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.domain.roles import (
    ContextItem,
    RoleError,
    RoleLimits,
    RoleName,
    RoleNotConfigured,
    RoleOutcome,
    RoleRequest,
)
from app.domain.severity import Severity
from app.providers.strand_bridge import StrandsSubagentExecutor


@pytest.fixture
def stub_entry(tmp_path: Path) -> Path:
    """Stub del subagente: parsea el stdin y REENVÍA answer.json sin parsearlo.

    Reenviar el contenido crudo es clave: permite probar lo que pasa cuando
    el subagente entrega una respuesta que no es JSON válido (la prueba
    ``test_invalid_json_answer_fails_explicitly``), sin que el propio stub
    crashee primero.
    """

    entry = tmp_path / "stub_entry.py"
    entry.write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "with open('answer.json', 'r', encoding='utf-8') as handle:\n"
        "    sys.stdout.write(handle.read())\n",
        encoding="utf-8",
    )
    return entry


@pytest.fixture
def executor(tmp_path: Path, stub_entry: Path) -> StrandsSubagentExecutor:
    executor = StrandsSubagentExecutor(
        Path(sys.executable),
        tmp_path,
        entry_script=stub_entry,
    )
    return executor


def make_request() -> RoleRequest:
    return RoleRequest(
        role=RoleName.CAUSE_ANALYSIS,
        instruction="analiza la evidencia y citar ids",
        context=(ContextItem(evidence_id="ev-1", snippet="return eval(x)"),),
        limits=RoleLimits(timeout_seconds=15.0),
    )


def write_answer(tmp_path: Path, answer: dict) -> None:
    (tmp_path / "answer.json").write_text(
        json.dumps(answer, ensure_ascii=False), encoding="utf-8"
    )


# -- camino feliz ----------------------------------------------------------


def test_completed_answer_maps_to_a_real_result(
    executor: StrandsSubagentExecutor, tmp_path: Path
) -> None:
    write_answer(
        tmp_path,
        {
            "outcome": "completed",
            "summary": "causa probable: entrada sin validar",
            "provider": "openrouter",
            "model": "stub-model",
            "proposals": [
                {
                    "title": "Ejecución dinámica",
                    "explanation": "eval con entrada externa.",
                    "severity": "high",
                    "evidence_ids": ["ev-1"],
                    "uncertainty": None,
                }
            ],
        },
    )

    result = executor.run(make_request())

    assert result.outcome is RoleOutcome.COMPLETED
    assert result.origin.value == "real"
    assert result.provider == "openrouter"
    assert result.model == "stub-model"
    assert result.proposals[0].severity is Severity.HIGH
    assert result.proposals[0].evidence_ids == ("ev-1",)


def test_evidence_ids_pass_through_and_validation_discards_foreign(
    executor: StrandsSubagentExecutor, tmp_path: Path
) -> None:
    """El adapter NO reescribe: deja que el harness descarte con trace."""

    write_answer(
        tmp_path,
        {
            "outcome": "completed",
            "summary": "citó evidencia ajena",
            "provider": "openrouter",
            "model": "stub-model",
            "proposals": [
                {
                    "title": "Propuesta con cita falsa",
                    "explanation": "cita ev-que-no-existe.",
                    "severity": "medium",
                    "evidence_ids": ["ev-que-no-existe"],
                    "uncertainty": None,
                }
            ],
        },
    )

    result = executor.run(make_request())

    assert result.proposals[0].evidence_ids == ("ev-que-no-existe",)


# -- errores declarados ----------------------------------------------------


def test_invalid_json_answer_fails_explicitly(
    executor: StrandsSubagentExecutor, tmp_path: Path
) -> None:
    (tmp_path / "answer.json").write_text("no-soy-json {", encoding="utf-8")

    result = executor.run(make_request())

    assert result.outcome is RoleOutcome.FAILED
    assert "JSON válido" in result.error


def test_nonzero_exit_fails_with_the_exit_code(
    executor: StrandsSubagentExecutor, tmp_path: Path
) -> None:
    crashing = tmp_path / "crash_entry.py"
    crashing.write_text(
        "import sys\nsys.stderr.write('fallo del subagente simulado')\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    executor = StrandsSubagentExecutor(
        Path(sys.executable), tmp_path, entry_script=crashing
    )

    result = executor.run(make_request())

    assert result.outcome is RoleOutcome.FAILED
    assert "código 2" in result.error
    assert "fallo del subagente simulado" in result.error


def test_timeout_fails_with_the_time_declared(
    stub_entry: Path, tmp_path: Path
) -> None:
    slow = tmp_path / "slow_entry.py"
    slow.write_text(
        "import time\ntime.sleep(5)\nprint('{\"outcome\": \"completed\"}')\n",
        encoding="utf-8",
    )
    executor = StrandsSubagentExecutor(
        Path(sys.executable), tmp_path, entry_script=slow
    )
    request = RoleRequest(
        role=RoleName.CAUSE_ANALYSIS,
        instruction="tarda más que el límite",
        limits=RoleLimits(timeout_seconds=1.0),
    )

    result = executor.run(request)

    assert result.outcome is RoleOutcome.FAILED
    assert "no respondió en 1.0 s" in result.error


def test_completed_without_provider_or_model_is_not_accepted(
    executor: StrandsSubagentExecutor, tmp_path: Path
) -> None:
    """Sin atribución real, el resultado no pasa por 'real' ni por 'mock'."""

    write_answer(
        tmp_path,
        {"outcome": "completed", "summary": "sin atribución", "proposals": []},
    )

    result = executor.run(make_request())

    assert result.outcome is RoleOutcome.FAILED
    assert "no identificó" in result.error


def test_refusal_still_carries_real_attribution(
    executor: StrandsSubagentExecutor, tmp_path: Path
) -> None:
    write_answer(
        tmp_path,
        {
            "outcome": "refused",
            "summary": "sin elementos suficientes",
            "provider": "openrouter",
            "model": "stub-model",
        },
    )

    result = executor.run(make_request())

    assert result.outcome is RoleOutcome.REFUSED
    assert result.proposals == ()
    assert result.origin.value == "real"


# -- construcción -------------------------------------------------------------


def test_missing_interpreter_fails_at_construction(tmp_path: Path) -> None:
    entry = tmp_path / "e.py"
    entry.write_text("print('{}')\n", encoding="utf-8")

    with pytest.raises(RoleNotConfigured, match="intérprete"):
        StrandsSubagentExecutor(
            tmp_path / "no-existe.exe", tmp_path, entry_script=entry
        )


def test_network_mode_request_is_rejected_by_the_domain() -> None:
    """Un rol sin red no admite ``allow_network``: el dominio lo rechaza al
    construir la petición, antes de que el ejecutor (y el subagente) existan.
    """

    with pytest.raises(
        RoleError, match="solo web_research puede ejecutarse con acceso a red"
    ):
        RoleRequest(
            role=RoleName.CAUSE_ANALYSIS,
            instruction="pedir red",
            limits=RoleLimits(timeout_seconds=5.0, allow_network=True),
        )
