"""Pruebas de frontera entre capas.

No comprueban comportamiento, sino que el dominio no arrastre dependencias que
el README prohíbe: FastAPI, SDK de proveedores o persistencia.
"""

from __future__ import annotations

import ast
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "app" / "domain"

FORBIDDEN_ROOTS = {
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlite3",
    "sqlalchemy",
    "anthropic",
    "openai",
    "google",
    "httpx",
    "requests",
}


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])

    return roots


# -- frontera del dominio -------------------------------------------------


def test_domain_has_no_forbidden_imports() -> None:
    offenders: dict[str, set[str]] = {}

    for path in DOMAIN_DIR.rglob("*.py"):
        bad = imported_roots(path) & FORBIDDEN_ROOTS
        if bad:
            offenders[path.name] = bad

    assert offenders == {}


# -- frontera del proveedor simulado --------------------------------------

MOCK_PROVIDER_PATH = Path(__file__).resolve().parents[2] / "app" / "providers" / "mock.py"

NETWORK_ROOTS = {"httpx", "requests", "socket", "urllib", "aiohttp", "ftplib"}


def test_mock_provider_imports_no_network_libraries() -> None:
    roots = imported_roots(MOCK_PROVIDER_PATH)

    assert roots & NETWORK_ROOTS == set()

