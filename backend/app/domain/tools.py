"""Contrato de herramientas deterministas de solo lectura.

QUÉ ES ESTO
-----------
Toda observación del proyecto auditado pasa por aquí: listar archivos, leer un
archivo o buscar un patrón. Las herramientas son código determinista; no
deciden políticas, no llaman al modelo y no interpretan hallazgos.

REGLAS QUE LA IMPLEMENTACIÓN DEBE RESPETAR
------------------------------------------
1. Leer únicamente dentro de la raíz autorizada, resolviendo la ruta real y
   bloqueando enlaces simbólicos que salgan de ella.
2. No escribir nada en el proyecto auditado, en ninguna circunstancia.
3. Aplicar las exclusiones por defecto y los límites recibidos. Si un límite
   corta la recolección, devolver ``TRUNCATED`` con la razón declarada: nunca
   presentar un resultado parcial como completo.
4. Reportar rutas relativas, nunca absolutas.
5. No ejecutar código del proyecto ni interpretar sus hooks o scripts.
6. Tratar el contenido leído como datos no confiables, jamás como instrucciones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from app.domain.evidence import Evidence
from app.domain.relative_paths import relative_path_problem


class ToolError(ValueError):
    """El contrato de herramienta se usó de forma inválida."""


class ToolNotAvailable(RuntimeError):
    """No hay implementación conectada para la herramienta solicitada."""


class ToolName(str, Enum):
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    PATTERN_SCAN = "pattern_scan"


class ToolOutcome(str, Enum):
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    FAILED = "failed"


# -- exclusiones por defecto --------------------------------------------

EXCLUDED_DIRECTORY_NAMES: Final = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "vendor",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        "coverage",
        ".next",
        ".nuxt",
        ".idea",
        ".vscode",
    }
)

EXCLUDED_FILE_NAMES: Final = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pgpass",
        "credentials",
        "secrets",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

SENSITIVE_FILE_NAMES: Final = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pgpass",
        "credentials",
        "secrets",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

SENSITIVE_SUFFIXES: Final = (".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".crt")

EXCLUDED_SUFFIXES: Final = (
    # credenciales y material criptográfico
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".jks",
    ".keystore",
    ".crt",
    # binarios y comprimidos
    ".pyc",
    ".pyo",
    ".class",
    ".jar",
    ".war",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".pdf",
    # multimedia y tipografías
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".mov",
)


def is_sensitive_path(relative_path: str) -> bool:
    """Identifica archivos cuyo contenido requiere autorización específica."""

    segments = relative_path.lower().split("/")
    file_name = segments[-1]
    return (
        file_name in SENSITIVE_FILE_NAMES
        or file_name.startswith(".env.")
        or file_name.endswith(SENSITIVE_SUFFIXES)
    )


def has_excluded_parent(relative_path: str) -> bool:
    """Evita habilitar secretos ubicados dentro de directorios excluidos."""

    return any(
        segment in EXCLUDED_DIRECTORY_NAMES
        for segment in relative_path.lower().split("/")[:-1]
    )


def is_excluded(relative_path: str) -> bool:
    """Indica si la ruta queda fuera del alcance por política, no por límite."""

    segments = relative_path.lower().split("/")
    if any(segment in EXCLUDED_DIRECTORY_NAMES for segment in segments[:-1]):
        return True

    file_name = segments[-1]
    if is_sensitive_path(relative_path):
        return True

    return file_name.endswith(EXCLUDED_SUFFIXES)


# -- límites -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """Valores iniciales conservadores; documentados en el README."""

    max_files: int = 200
    max_bytes_per_file: int = 64_000
    max_total_bytes: int = 512_000
    max_matches: int = 200
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        for field_name in (
            "max_files",
            "max_bytes_per_file",
            "max_total_bytes",
            "max_matches",
        ):
            if getattr(self, field_name) <= 0:
                raise ToolError(f"{field_name} debe ser positivo")
        if self.timeout_seconds <= 0:
            raise ToolError("timeout_seconds debe ser positivo")
        if self.max_bytes_per_file > self.max_total_bytes:
            raise ToolError(
                "max_bytes_per_file no puede superar max_total_bytes"
            )


# -- petición ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """Petición tipada: cada herramienta declara qué argumentos admite.

    No se usa un diccionario genérico de argumentos a propósito: un ``dict``
    abierto permite pasar cualquier cosa y traslada los errores a tiempo de
    ejecución.
    """

    tool: ToolName
    relative_path: str | None = None
    pattern: str | None = None
    limits: ToolLimits = field(default_factory=ToolLimits)
    allow_sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tool, ToolName):
            raise ToolError("tool debe ser un ToolName")
        if not isinstance(self.limits, ToolLimits):
            raise ToolError("limits debe ser un ToolLimits")
        if not isinstance(self.allow_sensitive, bool):
            raise ToolError("allow_sensitive debe ser booleano")

        if self.relative_path is not None:
            problem = relative_path_problem(self.relative_path)
            if problem is not None:
                raise ToolError(f"relative_path {problem}")
            sensitive_override = (
                self.allow_sensitive
                and self.tool is ToolName.READ_FILE
                and is_sensitive_path(self.relative_path)
                and not has_excluded_parent(self.relative_path)
            )
            if is_excluded(self.relative_path) and not sensitive_override:
                raise ToolError(
                    f"relative_path '{self.relative_path}' está excluida por política"
                )
        elif self.allow_sensitive:
            raise ToolError("allow_sensitive requiere relative_path")

        if self.pattern is not None:
            if not isinstance(self.pattern, str) or not self.pattern.strip():
                raise ToolError("pattern no puede estar vacío")
            if len(self.pattern) > 200:
                raise ToolError("pattern supera 200 caracteres")

        self._validate_arguments_for_tool()

    @property
    def is_whole_tree(self) -> bool:
        return self.relative_path is None

    def _validate_arguments_for_tool(self) -> None:
        if self.tool is ToolName.READ_FILE:
            if self.relative_path is None:
                raise ToolError("read_file exige relative_path")
            if self.pattern is not None:
                raise ToolError("read_file no admite pattern")
            if self.allow_sensitive and (
                not is_sensitive_path(self.relative_path)
                or has_excluded_parent(self.relative_path)
            ):
                raise ToolError(
                    "allow_sensitive solo habilita archivos sensibles "
                    "directamente dentro del alcance autorizado"
                )

        elif self.tool is ToolName.LIST_FILES:
            if self.allow_sensitive:
                raise ToolError("list_files no admite allow_sensitive")
            if self.pattern is not None:
                raise ToolError("list_files no admite pattern")

        elif self.tool is ToolName.PATTERN_SCAN:
            if self.allow_sensitive:
                raise ToolError("pattern_scan no admite allow_sensitive")
            if self.pattern is None:
                raise ToolError("pattern_scan exige pattern")


# -- resultado -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Resultado determinista: evidencias observadas y lo que quedó fuera."""

    tool: ToolName
    outcome: ToolOutcome
    evidence: tuple[Evidence, ...] = ()
    skipped: tuple[str, ...] = ()
    failure_reason: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tool, ToolName):
            raise ToolError("tool debe ser un ToolName")
        if not isinstance(self.outcome, ToolOutcome):
            raise ToolError("outcome debe ser un ToolOutcome")
        if any(not isinstance(item, Evidence) for item in self.evidence):
            raise ToolError("evidence solo admite Evidence")

        for skipped_path in self.skipped:
            problem = relative_path_problem(skipped_path)
            if problem is not None:
                raise ToolError(f"skipped: '{skipped_path}' {problem}")

        if any(not isinstance(item, str) or not item.strip() for item in self.limitations):
            raise ToolError("limitations no admite entradas vacías")

        if self.outcome is ToolOutcome.FAILED:
            if not isinstance(self.failure_reason, str) or not self.failure_reason.strip():
                raise ToolError("outcome=failed exige failure_reason")
            if self.evidence:
                raise ToolError("un resultado fallido no puede aportar evidencia")
        elif self.failure_reason is not None:
            raise ToolError("failure_reason solo se permite cuando outcome=failed")

        if self.outcome is ToolOutcome.TRUNCATED and not self.limitations:
            raise ToolError(
                "outcome=truncated exige declarar en limitations qué límite se alcanzó"
            )

    @property
    def is_complete(self) -> bool:
        return self.outcome is ToolOutcome.COMPLETED
