"""Carga de prompts conversacionales y de auditoría desde Markdown."""

from __future__ import annotations

import re
from functools import lru_cache
from importlib.resources import files

_SECTION_PATTERN = re.compile(r"^## ([a-z0-9_]+)\s*$", re.MULTILINE)


@lru_cache(maxsize=1)
def _prompt_sections() -> dict[str, str]:
    source = files(__package__).joinpath("agent_prompts.md").read_text(
        encoding="utf-8"
    )
    matches = list(_SECTION_PATTERN.finditer(source))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        content = source[match.end():end].strip()
        if not content:
            raise ValueError(f"la sección de prompt {match.group(1)!r} está vacía")
        sections[match.group(1)] = content
    return sections


def load_agent_prompt(name: str) -> str:
    """Obtiene un prompt identificado; falla explícitamente si no existe."""

    try:
        return _prompt_sections()[name]
    except KeyError as error:
        raise ValueError(f"no existe el prompt de AudiAI {name!r}") from error


def render_agent_prompt(name: str, **values: str) -> str:
    """Interpola campos conocidos sin reinterpretar el texto proporcionado."""

    template = load_agent_prompt(name)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"falta el valor {key!r} para el prompt {name!r}")
        return values[key]

    return re.sub(r"\{([a-z0-9_]+)\}", replace, template)
