"""Procedencia de un resultado: simulado o real.

Se comparte entre auditorías, resultados de roles y, más adelante, hallazgos.
Una simulación nunca se presenta como resultado real.
"""

from __future__ import annotations

from enum import Enum


class ResultOrigin(str, Enum):
    MOCK = "mock"
    REAL = "real"
