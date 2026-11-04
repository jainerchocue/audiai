"""Reglas de la entidad Evidencia: ruta relativa, líneas y fragmento acotado."""

from __future__ import annotations

import pytest

from app.domain.evidence import MAX_SNIPPET_CHARS, Evidence, EvidenceError


def evidence(**overrides) -> Evidence:
    params = {
        "evidence_id": "ev-001",
        "source_path": "src/app.py",
        "tool": "read_file",
        "snippet": "cursor.execute(query)",
    }
    params.update(overrides)
    return Evidence(**params)


def test_valid_evidence_with_line_range() -> None:
    item = evidence(line_start=42, line_end=44)

    assert item.location == "src/app.py:42-44"


def test_location_without_lines_is_just_the_path() -> None:
    assert evidence().location == "src/app.py"


def test_location_with_a_single_line() -> None:
    assert evidence(line_start=42, line_end=42).location == "src/app.py:42"


def test_location_with_open_ended_line_start() -> None:
    assert evidence(line_start=42).location == "src/app.py:42"


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "C:/Users/demo/proyecto/app.py",
        "src\\app.py",
        "../fuera.py",
        "src/../fuera.py",
        "src//app.py",
        "./src/app.py",
        "src/app.py/",
        " src/app.py",
        "",
    ],
)
def test_paths_outside_the_relative_contract_are_rejected(bad_path: str) -> None:
    with pytest.raises(EvidenceError):
        evidence(source_path=bad_path)


def test_null_byte_in_path_is_rejected() -> None:
    with pytest.raises(EvidenceError):
        evidence(source_path="src/app\x00.py")


@pytest.mark.parametrize("bad_tool", ["", "ReadFile", "read file", "read-file", "1tool"])
def test_tool_must_be_a_lowercase_identifier(bad_tool: str) -> None:
    with pytest.raises(EvidenceError):
        evidence(tool=bad_tool)


@pytest.mark.parametrize("bad_id", ["", "Ev-001", "ev 001", "ev/001", "a" * 65])
def test_invalid_evidence_id_is_rejected(bad_id: str) -> None:
    with pytest.raises(EvidenceError):
        evidence(evidence_id=bad_id)


def test_empty_snippet_is_rejected() -> None:
    with pytest.raises(EvidenceError):
        evidence(snippet="   ")


def test_snippet_has_a_hard_size_cap() -> None:
    with pytest.raises(EvidenceError):
        evidence(snippet="x" * (MAX_SNIPPET_CHARS + 1))

    assert len(evidence(snippet="x" * MAX_SNIPPET_CHARS).snippet) == MAX_SNIPPET_CHARS


@pytest.mark.parametrize(
    ("line_start", "line_end"),
    [
        (0, None),
        (-1, None),
        (10, 9),
        (None, 10),
        (True, None),
        (1.5, None),
    ],
)
def test_invalid_line_ranges_are_rejected(line_start, line_end) -> None:
    with pytest.raises(EvidenceError):
        evidence(line_start=line_start, line_end=line_end)


def test_limitations_must_be_non_empty_texts() -> None:
    assert evidence(limitations=("solo se leyó la primera coincidencia",)).limitations

    with pytest.raises(EvidenceError):
        evidence(limitations=("",))
