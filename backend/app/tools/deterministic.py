"""Herramientas deterministas de lectura acotada (Fase 3).

Implementan el contrato de ``app/domain/tools`` contra una raíz autorizada que
el llamante entrega en el constructor. Una instancia solo conoce esa raíz: no
recibe configuración adicional por petición.

Garantías implementadas aquí:

- Resolución: toda ruta se resuelve y se verifica que queda dentro de la raíz.
  Un enlace simbólico que salga de la raíz produce ``failed`` con un motivo que
  no revela rutas absolutas ni contenido de fuera.
- Solo lectura: este módulo no contiene una sola operación de escritura.
- Exclusiones: los archivos excluidos no se leen y su nombre no se reporta
  como ``skipped``; los secretos no acaban en el informe.
- Límites: ``max_files`` en listados y recorridos, ``max_bytes_per_file`` en
  lecturas, ``max_matches`` en búsquedas y snippet hard-capped a 4000
  caracteres (con TRUNCADO honesto cuando se alcanzan los límites).
- Identificadores: ``id_namespace`` separa las evidencias por recolección,
  para que dos comprobaciones no colisionen en un mismo índice.

Sobre el timeout: ``timeout_seconds`` forma parte del contrato, pero su
interrupción real pertenece al harness (Fase 6). Aquí no se fuerza.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.domain.evidence import MAX_SNIPPET_CHARS, Evidence
from app.domain.tools import (
    EXCLUDED_DIRECTORY_NAMES,
    ToolError,
    ToolName,
    ToolOutcome,
    ToolRequest,
    ToolResult,
    has_excluded_parent,
    is_excluded,
    is_sensitive_path,
)

_SKIPPED_SAMPLE_CAP = 20
_EVIDENCE_ID_PREFIX = {
    ToolName.LIST_FILES: "ev-list",
    ToolName.READ_FILE: "ev-read",
    ToolName.PATTERN_SCAN: "ev-scan",
}

_ID_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class _EscapeFromRoot(Exception):
    """Señal interna: la ruta resuelta sale de la raíz autorizada."""


def _validate_root(root: Path) -> None:
    """Valida que la raíz autorizada sea un directorio existente."""

    if not root.is_dir():
        raise ToolError("la raíz autorizada debe existir y ser un directorio")


class LocalReadOnlyTool:
    """Acceso de solo lectura y acotado a la raíz autorizada.

    ``id_namespace`` conduce el prefijo de las evidencias generadas por esta
    instancia (por ejemplo ``chk-dynamic-execution-ev-scan-0001``). Use una
    instancia por recolección: los identificadores se reinician en cada
    ``run`` a propósito.
    """

    def __init__(self, root: Path, *, id_namespace: str = "") -> None:
        _validate_root(root)
        if id_namespace and not _ID_NAMESPACE_PATTERN.match(id_namespace):
            raise ToolError(
                "id_namespace debe ser un identificador corto en minúsculas"
            )
        self._root = root.resolve()
        self._namespace = f"{id_namespace}-" if id_namespace else ""
        self._counter = 0

    def run(self, request: ToolRequest) -> ToolResult:
        """Ejecuta la herramienta solicitada. Nunca lanza por errores de I/O."""

        self._counter = 0
        try:
            if request.tool is ToolName.LIST_FILES:
                return self._list_files(request)
            if request.tool is ToolName.READ_FILE:
                return self._read_file(request)
            return self._pattern_scan(request)
        except _EscapeFromRoot:
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.FAILED,
                failure_reason="la ruta queda fuera de la raíz autorizada",
            )
        except FileNotFoundError:
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.FAILED,
                failure_reason="la ruta no existe dentro de la raíz autorizada",
            )
        except NotADirectoryError:
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.FAILED,
                failure_reason="la ruta no es un directorio dentro de la raíz autorizada",
            )
        except OSError:
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.FAILED,
                failure_reason="error del sistema de archivos durante la lectura",
            )

    # -- resolución -------------------------------------------------------

    def _resolve_within_root(self, relative: str) -> Path:
        candidate = (self._root / relative).resolve()
        if not candidate.is_relative_to(self._root):
            raise _EscapeFromRoot
        return candidate

    def _next_evidence_id(self, tool: ToolName) -> str:
        self._counter += 1
        return f"{self._namespace}{_EVIDENCE_ID_PREFIX[tool]}-{self._counter:04d}"

    def _relative_name(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()

    def list_sensitive_files(self, *, max_files: int = 200) -> tuple[str, ...]:
        """Lista rutas sensibles completas dentro del límite, sin leer contenido."""

        if max_files < 1:
            raise ToolError("max_files debe ser al menos 1")
        paths: list[str] = []
        for dir_name, dir_entries, file_entries in os.walk(
            self._root, followlinks=False
        ):
            base = Path(dir_name).resolve()
            if not base.is_relative_to(self._root):
                dir_entries[:] = []
                continue

            dir_entries[:] = sorted(
                name
                for name in dir_entries
                if name.lower() not in EXCLUDED_DIRECTORY_NAMES
                and not (base / name).is_symlink()
            )
            for file_name in sorted(file_entries):
                file_path = base / file_name
                if file_path.is_symlink():
                    continue
                relative = self._relative_name(file_path)
                if (
                    is_sensitive_path(relative)
                    and not has_excluded_parent(relative)
                ):
                    if len(paths) >= max_files:
                        raise ToolError(
                            "se encontraron más de "
                            f"{max_files} rutas confidenciales; limita la "
                            "carpeta para obtener un inventario completo"
                        )
                    paths.append(relative)
        return tuple(paths)

    # -- listados ---------------------------------------------------------

    def _scoped_files(
        self, relative_path: str | None
    ) -> tuple[list[Path], list[str]]:
        """Archivos válidos del subárbol, ordenados, y escapes detectados."""

        if relative_path is None:
            start: Path = self._root
        else:
            start = self._resolve_within_root(relative_path)
            if not start.exists():
                raise FileNotFoundError
            if not start.is_dir():
                raise NotADirectoryError

        files: list[Path] = []
        escapes: list[str] = []

        for dir_name, dir_entries, file_entries in os.walk(start, followlinks=False):
            base = Path(dir_name).resolve()
            if not base.is_relative_to(self._root):
                # candidato fuera de recorrida: no listamos ni bajamos más
                dir_entries[:] = []
                continue

            dir_entries[:] = sorted(
                name
                for name in dir_entries
                if name not in EXCLUDED_DIRECTORY_NAMES
                and not (base / name).is_symlink()
            )

            for file_name in sorted(file_entries):
                file_path = base / file_name
                if file_path.is_symlink():
                    escapes.append(self._relative_or_placeholder(file_path))
                    continue
                relative = self._relative_name(file_path)
                if not is_excluded(relative):
                    files.append(file_path)

        return files, escapes

    def _relative_or_placeholder(self, path: Path) -> str:
        try:
            return self._relative_name(path)
        except ValueError:
            return "<ruta fuera de la raíz>"

    def _list_files(self, request: ToolRequest) -> ToolResult:
        files, escapes = self._scoped_files(request.relative_path)

        evidence_items: list[Evidence] = []
        skipped: list[str] = []
        limitations: list[str] = []
        cut_by_limit = False

        for file_path in files:
            if len(evidence_items) >= request.limits.max_files:
                cut_by_limit = True
                if len(skipped) < _SKIPPED_SAMPLE_CAP:
                    skipped.append(self._relative_name(file_path))
                continue

            evidence_items.append(
                Evidence(
                    evidence_id=self._next_evidence_id(ToolName.LIST_FILES),
                    source_path=self._relative_name(file_path),
                    tool=request.tool.value,
                    snippet=(
                        f"archivo presente ({file_path.stat().st_size} bytes), "
                        "contenido aún sin leer"
                    ),
                )
            )

        if escapes:
            limitations.append(
                "se omitieron enlaces simbólicos por política conservadora"
            )
        if cut_by_limit:
            limitations.append(
                f"listado cortado en max_files={request.limits.max_files}; "
                "quedó una parte del árbol sin listar (muestra limitada a "
                f"{_SKIPPED_SAMPLE_CAP} rutas)"
            )
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.TRUNCATED,
                evidence=tuple(evidence_items),
                skipped=tuple(skipped),
                limitations=tuple(limitations),
            )

        return ToolResult(
            tool=request.tool,
            outcome=ToolOutcome.COMPLETED,
            evidence=tuple(evidence_items),
            limitations=tuple(limitations),
        )

    # -- lectura ----------------------------------------------------------

    def _read_file(self, request: ToolRequest) -> ToolResult:
        if request.relative_path is None:  # defensivo: el contrato ya lo exige
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.FAILED,
                failure_reason="falta relative_path",
            )

        candidate = self._root
        for segment in request.relative_path.split("/"):
            candidate = candidate / segment
            if candidate.is_symlink():
                return ToolResult(
                    tool=request.tool,
                    outcome=ToolOutcome.FAILED,
                    failure_reason="se omitió un enlace simbólico por política",
                )

        target = self._resolve_within_root(request.relative_path)

        if not target.is_file():
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.FAILED,
                failure_reason=(
                    f"no es un archivo regular dentro de la raíz: "
                    f"{request.relative_path}"
                ),
            )

        byte_limit = request.limits.max_bytes_per_file
        with open(target, "rb") as handle:
            raw = handle.read(byte_limit)
            has_more = bool(handle.read(1))

        limitations: list[str] = []
        truncated = has_more

        if has_more:
            limitations.append(
                f"lectura cortada a {byte_limit} bytes por max_bytes_per_file"
            )

        text = raw.decode("utf-8", errors="replace")
        if len(text) > MAX_SNIPPET_CHARS:
            text = text[:MAX_SNIPPET_CHARS]
            truncated = True
            limitations.append(f"fragmento corto a {MAX_SNIPPET_CHARS} caracteres")

        if "\ufffd" in text:
            limitations.append(
                "decodificación con reemplazos: posible archivo no utf-8"
            )

        evidence = Evidence(
            evidence_id=self._next_evidence_id(ToolName.READ_FILE),
            source_path=request.relative_path,
            tool=request.tool.value,
            snippet=text,
            limitations=tuple(limitations),
        )

        if truncated:
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.TRUNCATED,
                evidence=(evidence,),
                limitations=tuple(limitations),
            )

        return ToolResult(
            tool=request.tool,
            outcome=ToolOutcome.COMPLETED,
            evidence=(evidence,),
            limitations=tuple(limitations),
        )

    # -- búsqueda ---------------------------------------------------------

    def _pattern_scan(self, request: ToolRequest) -> ToolResult:
        assert request.pattern is not None  # el contrato garantiza el pattern
        try:
            pattern = re.compile(request.pattern)
        except re.error:
            return ToolResult(
                tool=request.tool,
                outcome=ToolOutcome.FAILED,
                failure_reason="el pattern no es una expresión regular válida",
            )

        files, escapes = self._scoped_files(request.relative_path)
        evidence_items: list[Evidence] = []
        skipped: list[str] = []
        limitations: list[str] = []
        cut_by_files = len(files) > request.limits.max_files
        cut_by_matches = False
        any_line_cut = False

        for index, file_path in enumerate(files):
            if index >= request.limits.max_files:
                if len(skipped) < _SKIPPED_SAMPLE_CAP:
                    skipped.append(self._relative_name(file_path))
                continue

            if len(evidence_items) >= request.limits.max_matches:
                cut_by_matches = True
                if len(skipped) < _SKIPPED_SAMPLE_CAP:
                    skipped.append(self._relative_name(file_path))
                continue

            relative = self._relative_name(file_path)

            with open(file_path, "rb") as handle:
                raw = handle.read(request.limits.max_bytes_per_file)

            text = raw.decode("utf-8", errors="replace")
            if "\ufffd" in text:
                limitations.append("decodificación con reemplazos: archivo no utf-8")

            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line) is None:
                    continue
                if len(evidence_items) >= request.limits.max_matches:
                    cut_by_matches = True
                    break

                snippet = line[:MAX_SNIPPET_CHARS]
                if len(line) > MAX_SNIPPET_CHARS:
                    any_line_cut = True
                evidence_items.append(
                    Evidence(
                        evidence_id=self._next_evidence_id(ToolName.PATTERN_SCAN),
                        source_path=relative,
                        tool=request.tool.value,
                        snippet=snippet,
                        line_start=line_number,
                    )
                )

        if escapes:
            limitations.append(
                "se omitieron enlaces simbólicos por política conservadora"
            )
        if any_line_cut:
            limitations.append(
                f"algunas líneas recortadas a {MAX_SNIPPET_CHARS} caracteres"
            )
        if cut_by_files:
            limitations.append(
                f"búsqueda cortada en max_files={request.limits.max_files}"
            )
        if cut_by_matches:
            limitations.append(
                f"búsqueda cortada en max_matches={request.limits.max_matches}"
            )

        truncated = cut_by_files or cut_by_matches
        return ToolResult(
            tool=request.tool,
            outcome=ToolOutcome.TRUNCATED if truncated else ToolOutcome.COMPLETED,
            evidence=tuple(evidence_items),
            skipped=tuple(skipped),
            limitations=tuple(limitations),
        )
