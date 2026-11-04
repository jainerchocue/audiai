"""Severidad propuesta para un hallazgo.

``UNKNOWN`` existe para que un rol pueda declarar honestamente que no sabe
graduar el riesgo en lugar de inventar una severidad.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
    UNKNOWN = "unknown"
