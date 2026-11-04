"""Exportación Markdown de una sesión de auditoría completada."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.domain.tools import is_sensitive_path
from app.services.agent_chat import ChatSession

_SEVERITY_LABELS = {
    "critical": "Crítica",
    "high": "Alta",
    "medium": "Media",
    "low": "Baja",
    "info": "Informativa",
    "unknown": "Sin determinar",
}


def build_session_readme(session: ChatSession) -> str:
    """Construye el README sin exponer rutas absolutas ni fragmentos de código."""

    evidence_by_id = {item.evidence_id: item for item in session.evidence}
    sensitive_ids = {
        item.evidence_id
        for item in session.evidence
        if is_sensitive_path(item.source_path)
    }

    findings = [
        item
        for item in session.findings
        if not sensitive_ids.intersection(item.evidence_ids)
    ]
    docs = [
        item
        for item in session.docs
        if not sensitive_ids.intersection(item.evidence_ids)
    ]
    patches = [
        item
        for item in session.patches
        if not sensitive_ids.intersection(item.evidence_ids)
    ]
    referenced_ids = {
        evidence_id
        for item in findings
        for evidence_id in item.evidence_ids
    }
    referenced_ids.update(
        evidence_id for item in docs for evidence_id in item.evidence_ids
    )
    referenced_ids.update(
        evidence_id for item in patches for evidence_id in item.evidence_ids
    )
    evidence = [
        evidence_by_id[evidence_id]
        for evidence_id in sorted(referenced_ids)
        if evidence_id in evidence_by_id and evidence_id not in sensitive_ids
    ]

    project_name = session.scope_root.name if session.scope_root else "Proyecto local"
    if not project_name:
        project_name = "Proyecto local"
    mode = "Agente" if session.agent_mode == "agent" else "Multiagente"
    provider = session.analysis_provider or session.chat_provider
    model = session.analysis_model or session.chat_model
    model_label = f"{provider} · {model}" if provider and model else model or provider or "No especificado"

    lines = [
        "# Informe de auditoría AudiAI",
        "",
        f"> Generado: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "## Resumen ejecutivo",
        "",
        f"- **Proyecto:** {project_name}",
        f"- **Modo:** {mode}",
        f"- **Estado:** {session.state}",
        f"- **Hallazgos revisables incluidos:** {len(findings)}",
        f"- **Evidencias referenciadas:** {len(evidence)}",
        f"- **Modelo/proveedor:** {model_label}",
        f"- **Permiso de lectura sensible concedido:** {'Sí' if session.sensitive_read_approved else 'No'}",
        f"- **Consentimiento para proveedor externo:** {'Sí' if session.external_data_consent else 'No'}",
        "",
        "Este informe resume los resultados disponibles en esta sesión; no representa una auditoría exhaustiva ni una garantía de seguridad.",
        "",
        "## Hallazgos",
        "",
    ]

    if findings:
        for finding in findings:
            severity = _SEVERITY_LABELS.get(
                finding.severity.value, "Sin determinar"
            )
            status = "Con evidencia" if finding.is_confirmed else "Hipótesis"
            lines.extend(
                [
                    f"### {finding.title}",
                    "",
                    f"- **Severidad propuesta:** {severity}",
                    f"- **Estado:** {status}",
                    f"- **Origen:** {finding.origin.value}",
                    f"- **Evidencias:** {', '.join(finding.evidence_ids) or 'Ninguna'}",
                    "",
                    finding.explanation,
                    "",
                ]
            )
            if finding.recommendation:
                lines.extend(["**Recomendación:**", "", finding.recommendation, ""])
            if finding.uncertainty:
                lines.extend(["**Incertidumbre:**", "", finding.uncertainty, ""])
            if finding.limitations:
                lines.extend(["**Limitaciones:**", ""])
                lines.extend(f"- {item}" for item in finding.limitations)
                lines.append("")
    else:
        lines.extend(["No hay hallazgos disponibles para incluir.", ""])

    if docs:
        lines.extend(["## Informe técnico", ""])
        for section in docs:
            lines.extend(
                [
                    f"### {section.title}",
                    "",
                    section.body,
                    "",
                    f"- **Origen:** {section.origin}",
                ]
            )
            if section.evidence_ids:
                lines.append(
                    f"- **Evidencias:** {', '.join(section.evidence_ids)}"
                )
            lines.append("")

    if patches:
        lines.extend(["## Recomendaciones de remediación", ""])
        for patch in patches:
            lines.extend(
                [
                    f"### {patch.title}",
                    "",
                    patch.description,
                    "",
                    f"- **Hallazgo relacionado:** {patch.finding_id}",
                    f"- **Origen:** {patch.origin}",
                ]
            )
            if patch.evidence_ids:
                lines.append(f"- **Evidencias:** {', '.join(patch.evidence_ids)}")
            if patch.diff:
                lines.extend(["", "~~~~diff", patch.diff, "~~~~"])
            lines.append("")

    lines.extend(["## Evidencias", ""])
    if evidence:
        for item in evidence:
            location = item.location
            lines.append(
                f"- **{item.evidence_id}** — `{location}` ({item.tool})"
            )
            lines.extend(f"  - Limitación: {limit}" for limit in item.limitations)
    else:
        lines.append("No hay evidencias no confidenciales citadas en el informe.")
    if len(sensitive_ids):
        lines.extend(
            [
                "",
                f"> Se omitieron {len(sensitive_ids)} evidencia(s) de archivos confidenciales, junto con los resultados que dependían de ellas. El contenido no se incluye en este archivo.",
            ]
        )

    lines.extend(
        [
            "",
            "## Alcance y limitaciones",
            "",
            "- AudiAI realizó una revisión de solo lectura y no aplicó cambios al proyecto.",
            "- Las severidades son propuestas; requieren revisión humana y no confirman por sí solas la explotabilidad.",
            "- El informe no contiene fragmentos de archivos ni rutas absolutas.",
            "",
        ]
    )
    return "\n".join(lines)
