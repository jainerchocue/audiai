"""Adaptadores de IA y ejecutores de roles.

Aquí se conectan las implementaciones reales: proveedor de modelos y
ejecutores de roles (subagentes). Este paquete no decide permisos, no valida
evidencia y no accede por sí mismo al proyecto auditado: recibe contexto
acotado del harness y devuelve propuestas que el harness valida.

Estado actual: vacío a propósito. Solo se incluye un conector nulo que falla
en cerrado, para que ningún rol sin implementar se presente como funcionando.
"""
