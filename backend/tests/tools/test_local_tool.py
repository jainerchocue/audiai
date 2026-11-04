"""Fase 3: herramientas reales contra fixtures temporales.

Toda la fixture se crea en ``tmp_path`` por pytest: ninguno de estos casos
toca el repositorio ni el proyecto auditado. Los casos de escape con enlaces
simbólicos saltan limpiamente si el entorno no permite crearlos.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.tools import (
    ToolError,
    ToolLimits,
    ToolName,
    ToolOutcome,
    ToolRequest,
    ToolResult,
)
from app.tools.deterministic import LocalReadOnlyTool


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """El "proyecto auditado" de todos estos casos: código, docs, y un secreto."""

    root = tmp_path / "audit_target"
    (root / "src").mkdir(parents=True)
    (root / "config").mkdir()

    (root / "src" / "app.py").write_text(
        "import os\n\n\ndef run(value):\n    return eval(value)\n",
        encoding="utf-8",
    )
    (root / "src" / "utils.py").write_text(
        "def helper(value):\n    return value * 2\n\n\nMAGIC = eval('1')\n",
        encoding="utf-8",
    )
    (root / "src" / "big.py").write_text("x" * 5_000, encoding="utf-8")
    (root / "docs.md").write_text(
        "documentación del proyecto demo\n", encoding="utf-8"
    )
    (root / "config" / ".env").write_text("SECRET=never-read\n", encoding="utf-8")
    return root


@pytest.fixture
def tool(tree: Path) -> LocalReadOnlyTool:
    return LocalReadOnlyTool(tree)


# -- construcción ---------------------------------------------------------


def test_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="directorio"):
        LocalReadOnlyTool(tmp_path / "no-existe")

    flat_file = tmp_path / "archivo.txt"
    flat_file.write_text("x", encoding="utf-8")
    with pytest.raises(ToolError, match="directorio"):
        LocalReadOnlyTool(flat_file)


# -- lectura de archivo ---------------------------------------------------


def read_file(tool: LocalReadOnlyTool, relative: str, **overrides) -> ToolResult:
    request = ToolRequest(tool=ToolName.READ_FILE, relative_path=relative, **overrides)
    return tool.run(request)


def test_read_file_returns_content_with_a_verifiable_location(
    tool: LocalReadOnlyTool,
) -> None:
    result = read_file(tool, "src/app.py")

    assert result.outcome is ToolOutcome.COMPLETED
    assert result.failure_reason is None

    evidence = result.evidence[0]
    assert "return eval(value)" in evidence.snippet
    assert evidence.location == "src/app.py"
    assert evidence.tool == "read_file"


def test_read_file_on_exact_byte_limit_is_complete(tool: LocalReadOnlyTool) -> None:
    result = read_file(tool, "src/app.py", limits=ToolLimits(max_bytes_per_file=100_000))

    assert result.outcome is ToolOutcome.COMPLETED


def test_read_file_cuts_at_byte_limit_and_declares_it(
    tool: LocalReadOnlyTool,
) -> None:
    result = read_file(tool, "src/big.py", limits=ToolLimits(max_bytes_per_file=10))

    assert result.outcome is ToolOutcome.TRUNCATED
    assert result.evidence[0].snippet == "x" * 10
    assert any("max_bytes_per_file" in note for note in result.limitations)


def test_read_file_shortens_long_snippets_and_declares_it(
    tool: LocalReadOnlyTool,
) -> None:
    result = read_file(tool, "src/big.py")

    assert result.outcome is ToolOutcome.TRUNCATED
    assert result.evidence[0].snippet == "x" * 4_000
    assert any("fragmento" in note for note in result.limitations)


def test_read_file_on_a_missing_path_fails_without_evidence(
    tool: LocalReadOnlyTool,
) -> None:
    result = read_file(tool, "src/nope.py")

    assert result.outcome is ToolOutcome.FAILED
    assert result.evidence == ()
    assert "nope.py" in result.failure_reason


def test_read_file_on_a_directory_fails(tool: LocalReadOnlyTool) -> None:
    result = read_file(tool, "src")

    assert result.outcome is ToolOutcome.FAILED
    assert "regular" in result.failure_reason


def test_non_utf8_file_is_reported_with_replacements(
    tree: Path, tool: LocalReadOnlyTool
) -> None:
    (tree / "config" / "binary.cfg").write_bytes(b"\xff\xfe\x00")

    result = read_file(tool, "config/binary.cfg")

    assert result.outcome is ToolOutcome.COMPLETED
    assert any("utf-8" in note for note in result.limitations)
    assert "\ufffd" in result.evidence[0].snippet


# -- listado ---------------------------------------------------------------


def list_files(tool: LocalReadOnlyTool, **overrides) -> ToolResult:
    request = ToolRequest(tool=ToolName.LIST_FILES, **overrides)
    return tool.run(request)


def test_list_files_reports_the_tree_and_hides_excluded_secrets(
    tool: LocalReadOnlyTool,
) -> None:
    result = list_files(tool)

    assert result.outcome is ToolOutcome.COMPLETED
    reported = {item.source_path for item in result.evidence}

    assert "docs.md" in reported
    assert "src/app.py" in reported
    assert "src/utils.py" in reported
    assert all("bytes" in item.snippet for item in result.evidence)
    # una exclusión por política no se reporta ni en evidencia ni en skipped
    assert "config/.env" not in reported
    assert "config/.env" not in result.skipped


def test_list_files_skips_dependency_directories(
    tree: Path, tool: LocalReadOnlyTool
) -> None:
    dependencies = tree / "node_modules" / "react"
    dependencies.mkdir(parents=True)
    (dependencies / "index.js").write_text("module.exports = 1\n", encoding="utf-8")

    result = list_files(tool)

    reported = {item.source_path for item in result.evidence}
    assert not any("node_modules" in path for path in reported)


def test_list_files_cuts_at_max_files_and_reports_it(
    tool: LocalReadOnlyTool,
) -> None:
    result = list_files(tool, limits=ToolLimits(max_files=2))

    assert result.outcome is ToolOutcome.TRUNCATED
    assert len(result.evidence) == 2
    assert result.skipped
    assert any("max_files" in note for note in result.limitations)


def test_an_empty_tree_yields_a_completed_empty_result(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = LocalReadOnlyTool(empty).run(ToolRequest(tool=ToolName.LIST_FILES))

    # un listado vacío es honesto aquí porque el árbol está vacío;
    # un truncamiento, en cambio, nunca podría afirmarlo (regla del contrato)
    assert result.outcome is ToolOutcome.COMPLETED
    assert result.evidence == ()


# -- búsqueda --------------------------------------------------------------


def scan_subtree(tool: LocalReadOnlyTool, pattern: str) -> ToolResult:
    request = ToolRequest(
        tool=ToolName.PATTERN_SCAN, pattern=pattern, relative_path="src"
    )
    return tool.run(request)


def scan_tree(tool: LocalReadOnlyTool, pattern: str, **overrides) -> ToolResult:
    request = ToolRequest(tool=ToolName.PATTERN_SCAN, pattern=pattern, **overrides)
    return tool.run(request)


def test_scan_finds_matches_with_line_numbers(tool: LocalReadOnlyTool) -> None:
    result = scan_subtree(tool, pattern=r"eval\(")

    assert result.outcome is ToolOutcome.COMPLETED
    places = {
        (evidence.source_path, evidence.line_start)
        for evidence in result.evidence
    }
    assert ("src/app.py", 5) in places
    assert ("src/utils.py", 5) in places
    assert all("eval(" in evidence.snippet for evidence in result.evidence)


def test_scan_cuts_at_max_matches_and_declares_it(tool: LocalReadOnlyTool) -> None:
    result = scan_tree(tool, pattern=r"eval\(", limits=ToolLimits(max_matches=1))

    assert result.outcome is ToolOutcome.TRUNCATED
    assert len(result.evidence) == 1
    assert any("max_matches" in note for note in result.limitations)


def test_scan_rejects_an_uncompilable_pattern(tool: LocalReadOnlyTool) -> None:
    result = scan_subtree(tool, pattern="eval((")

    assert result.outcome is ToolOutcome.FAILED
    assert "expresión regular" in result.failure_reason
    assert result.evidence == ()


def test_scan_on_a_missing_subtree_fails(tool: LocalReadOnlyTool) -> None:
    result = scan_tree(tool, pattern="eval", relative_path="src/nope-dir")

    assert result.outcome is ToolOutcome.FAILED
    assert "no existe" in result.failure_reason


def test_scan_skips_symlinked_files_outside_the_root(
    tree: Path, tool: LocalReadOnlyTool, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("token eval leak\n", encoding="utf-8")

    try:
        (tree / "src" / "linked.py").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("entorno sin permisos para enlaces simbólicos")

    result = scan_tree(tool, pattern="eval")

    snippets = [evidence.snippet for evidence in result.evidence]
    assert all("token" not in snippet for snippet in snippets)
    assert any("enlaces simbólicos" in note for note in result.limitations)

# -- identidad, fugas y frontera ------------------------------------------


def test_scan_evidence_ids_are_unique_and_prefixed(tool: LocalReadOnlyTool) -> None:
    result = scan_subtree(tool, pattern=r"\w")

    identifiers = [evidence.evidence_id for evidence in result.evidence]
    assert len(identifiers) == len(set(identifiers))
    assert all(identifier.startswith("ev-scan-") for identifier in identifiers)


def test_results_never_leak_the_absolute_root(tree: Path, tool: LocalReadOnlyTool) -> None:
    root_text = str(tree)

    results = [
        list_files(tool),
        read_file(tool, "src/app.py"),
        scan_subtree(tool, pattern="eval"),
    ]

    for result in results:
        exposed = [
            *(evidence.snippet for evidence in result.evidence),
            *(evidence.source_path for evidence in result.evidence),
            *result.skipped,
            *result.limitations,
            result.failure_reason or "",
        ]
        assert all(root_text not in piece for piece in exposed)


def test_excluded_paths_never_reach_the_tool(tool: LocalReadOnlyTool) -> None:
    # la validación del contrato rechaza la ruta antes de que la herramienta exista
    with pytest.raises(ToolError):
        ToolRequest(tool=ToolName.READ_FILE, relative_path="config/.env")


def test_sensitive_files_are_listed_by_name_without_reading_content(
    tool: LocalReadOnlyTool,
) -> None:
    assert tool.list_sensitive_files() == ("config/.env",)
    listed = list_files(tool)
    assert all(item.source_path != "config/.env" for item in listed.evidence)


def test_sensitive_file_inventory_reports_when_it_exceeds_its_limit(
    tree: Path,
) -> None:
    (tree / ".env.one").write_text("first", encoding="utf-8")
    (tree / ".env.two").write_text("second", encoding="utf-8")
    tool = LocalReadOnlyTool(tree)

    with pytest.raises(ToolError, match="más de 1 rutas confidenciales"):
        tool.list_sensitive_files(max_files=1)


def test_sensitive_file_requires_explicit_override_and_returns_evidence(
    tool: LocalReadOnlyTool,
) -> None:
    with pytest.raises(ToolError, match="excluida"):
        ToolRequest(tool=ToolName.READ_FILE, relative_path="config/.env")

    result = read_file(tool, "config/.env", allow_sensitive=True)

    assert result.outcome is ToolOutcome.COMPLETED
    assert result.evidence[0].snippet.replace("\r\n", "\n") == "SECRET=never-read\n"


def test_sensitive_override_cannot_read_normal_or_excluded_tree_files(
    tool: LocalReadOnlyTool,
) -> None:
    with pytest.raises(ToolError, match="solo habilita archivos sensibles"):
        ToolRequest(
            tool=ToolName.READ_FILE,
            relative_path="src/app.py",
            allow_sensitive=True,
        )
    with pytest.raises(ToolError):
        ToolRequest(
            tool=ToolName.READ_FILE,
            relative_path=".git/config/.env",
            allow_sensitive=True,
        )


def test_direct_traversal_is_rejected_by_the_contract(tool: LocalReadOnlyTool) -> None:
    with pytest.raises(ToolError, match=r"\.\."):
        ToolRequest(tool=ToolName.READ_FILE, relative_path="../opencode.json")
