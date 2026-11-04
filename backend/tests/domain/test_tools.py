"""Contrato de herramientas de solo lectura: exclusiones, límites y resultados.

Estas pruebas fijan lo que la Fase 3 no podrá relajar sin revisión: qué queda
excluido por política, qué límites existen y cómo se declara un resultado
parcial.
"""

from __future__ import annotations

import pytest

from app.domain.evidence import Evidence
from app.domain.tools import (
    EXCLUDED_DIRECTORY_NAMES,
    EXCLUDED_FILE_NAMES,
    EXCLUDED_SUFFIXES,
    ToolError,
    ToolLimits,
    ToolName,
    ToolNotAvailable,
    ToolOutcome,
    ToolRequest,
    ToolResult,
    is_excluded,
)
from app.tools.unavailable import UnavailableTool


# -- exclusiones por política --------------------------------------------


@pytest.mark.parametrize(
    "excluded_path",
    [
        ".env",
        ".env.local",
        "config/.env.production",
        "keys/server.pem",
        "deploy/tls.key",
        "certs/chain.p12",
        ".git/config",
        "node_modules/react/index.js",
        "__pycache__/app.cpython-315.pyc",
        "dist/bundle.js",
        "ssh/id_rsa",
        "scripts/pdfgen.exe",
    ],
)
def test_paths_excluded_by_policy(excluded_path: str) -> None:
    assert is_excluded(excluded_path) is True


@pytest.mark.parametrize(
    "allowed_path",
    [
        "src/app.py",
        "src/main.rs",
        "README.md",
        "deploy/deployment.yaml",
    ],
)
def test_paths_inside_scope(allowed_path: str) -> None:
    assert is_excluded(allowed_path) is False


def test_exclusion_names_are_lowercase() -> None:
    assert all(name == name.lower() for name in EXCLUDED_DIRECTORY_NAMES)
    assert all(name == name.lower() for name in EXCLUDED_FILE_NAMES)
    assert all(suffix == suffix.lower() for suffix in EXCLUDED_SUFFIXES)


def test_secret_files_are_excluded_by_name_or_suffix() -> None:
    assert ".env" in EXCLUDED_FILE_NAMES
    assert ".pem" in EXCLUDED_SUFFIXES
    assert ".key" in EXCLUDED_SUFFIXES


# -- límites --------------------------------------------------------------


def test_default_limits_are_conservative() -> None:
    limits = ToolLimits()

    assert limits.max_files == 200
    assert limits.max_bytes_per_file == 64_000
    assert limits.max_total_bytes == 512_000
    assert limits.max_matches == 200
    assert limits.timeout_seconds == 10.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_files": 0},
        {"max_bytes_per_file": -1},
        {"max_total_bytes": 0},
        {"max_matches": 0},
        {"timeout_seconds": 0},
    ],
)
def test_limits_must_be_positive(kwargs: dict) -> None:
    with pytest.raises(ToolError):
        ToolLimits(**kwargs)


def test_file_limit_cannot_exceed_the_total() -> None:
    with pytest.raises(ToolError):
        ToolLimits(max_bytes_per_file=600_000, max_total_bytes=512_000)


# -- petición -------------------------------------------------------------


def test_list_files_accepts_the_whole_tree() -> None:
    request = ToolRequest(tool=ToolName.LIST_FILES)

    assert request.is_whole_tree is True


def test_read_file_requires_a_path() -> None:
    with pytest.raises(ToolError, match="exige relative_path"):
        ToolRequest(tool=ToolName.READ_FILE)


def test_read_file_rejects_pattern() -> None:
    with pytest.raises(ToolError, match="no admite pattern"):
        ToolRequest(
            tool=ToolName.READ_FILE,
            relative_path="src/app.py",
            pattern="def ",
        )


def test_pattern_scan_requires_a_pattern() -> None:
    with pytest.raises(ToolError, match="exige pattern"):
        ToolRequest(tool=ToolName.PATTERN_SCAN)


def test_pattern_has_a_length_cap() -> None:
    with pytest.raises(ToolError, match="supera 200"):
        ToolRequest(tool=ToolName.PATTERN_SCAN, pattern="x" * 201)


@pytest.mark.parametrize("blank_pattern", ["", "   "])
def test_pattern_must_have_content(blank_pattern: str) -> None:
    with pytest.raises(ToolError):
        ToolRequest(tool=ToolName.PATTERN_SCAN, pattern=blank_pattern)


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "C:/secretos/api.env",
        "src\\app.py",
        "../app.py",
        "src/../app.py",
        "~/.ssh/id_rsa",
        "src/config/.env",
    ],
)
def test_requests_reject_paths_outside_the_tree_or_excluded(bad_path: str) -> None:
    with pytest.raises(ToolError):
        ToolRequest(tool=ToolName.READ_FILE, relative_path=bad_path)


def test_list_files_rejects_pattern() -> None:
    with pytest.raises(ToolError, match="no admite pattern"):
        ToolRequest(tool=ToolName.LIST_FILES, pattern="def ")


# -- resultado ------------------------------------------------------------


def evidence_item() -> Evidence:
    return Evidence(
        evidence_id="ev-001",
        source_path="src/app.py",
        tool="pattern_scan",
        snippet="eval(team_id)",
    )


def test_completed_result_reports_evidence() -> None:
    result = ToolResult(
        tool=ToolName.PATTERN_SCAN,
        outcome=ToolOutcome.COMPLETED,
        evidence=(evidence_item(),),
    )

    assert result.is_complete is True


def test_failed_result_requires_a_reason_and_no_evidence() -> None:
    with pytest.raises(ToolError, match="exige failure_reason"):
        ToolResult(tool=ToolName.PATTERN_SCAN, outcome=ToolOutcome.FAILED)

    with pytest.raises(ToolError, match="no puede aportar evidencia"):
        ToolResult(
            tool=ToolName.PATTERN_SCAN,
            outcome=ToolOutcome.FAILED,
            failure_reason="la raíz autorizada no existe",
            evidence=(evidence_item(),),
        )

    failed = ToolResult(
        tool=ToolName.PATTERN_SCAN,
        outcome=ToolOutcome.FAILED,
        failure_reason="la raíz autorizada no existe",
    )

    assert failed.evidence == ()


def test_failure_reason_is_forbidden_when_not_failed() -> None:
    with pytest.raises(ToolError, match="solo se permite"):
        ToolResult(
            tool=ToolName.LIST_FILES,
            outcome=ToolOutcome.COMPLETED,
            failure_reason="nunca aquí",
        )


def test_truncated_result_must_declare_why() -> None:
    with pytest.raises(ToolError, match="limitations"):
        ToolResult(
            tool=ToolName.LIST_FILES,
            outcome=ToolOutcome.TRUNCATED,
            skipped=("src/big.py",),
        )

    truncated = ToolResult(
        tool=ToolName.LIST_FILES,
        outcome=ToolOutcome.TRUNCATED,
        skipped=("src/big.py",),
        limitations=("se alcanzó max_files; falta listar el resto del árbol",),
    )

    assert truncated.is_complete is False


@pytest.mark.parametrize("bad_skipped", ["/app.py", "C:/x.py", "..", ""])
def test_skipped_paths_follow_the_same_relative_rules(bad_skipped: str) -> None:
    with pytest.raises(ToolError):
        ToolResult(
            tool=ToolName.LIST_FILES,
            outcome=ToolOutcome.TRUNCATED,
            skipped=(bad_skipped,),
            limitations=("corte por límite",),
        )


def test_result_rejects_non_evidence_items() -> None:
    with pytest.raises(ToolError, match="evidence solo admite Evidence"):
        ToolResult(
            tool=ToolName.PATTERN_SCAN,
            outcome=ToolOutcome.COMPLETED,
            evidence=("ev-001",),
        )


# -- conector vacío -------------------------------------------------------


def test_unavailable_tool_fails_closed() -> None:
    executor = UnavailableTool(
        ToolName.READ_FILE, reason="la fase 3 no está implementada"
    )

    request = ToolRequest(
        tool=ToolName.READ_FILE, relative_path="src/app.py"
    )

    with pytest.raises(ToolNotAvailable) as excinfo:
        executor.run(request)

    assert "no disponible" in str(excinfo.value)


def test_unavailable_tool_refuses_a_foreign_request() -> None:
    executor = UnavailableTool(ToolName.READ_FILE)

    with pytest.raises(ToolNotAvailable, match="esta instancia representa 'read_file'"):
        executor.run(ToolRequest(tool=ToolName.LIST_FILES))
