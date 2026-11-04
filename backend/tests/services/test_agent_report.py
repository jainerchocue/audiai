from pathlib import Path

from app.domain.evidence import Evidence
from app.domain.finding import Finding, FindingOrigin, FindingStatus
from app.domain.severity import Severity
from app.services.agent_chat import ChatSession
from app.services.agent_report import build_session_readme


def test_report_omits_sensitive_evidence_and_absolute_project_path() -> None:
    session = ChatSession("chat-0001")
    session.state = "completa"
    session.scope_root = Path("C:/Users/person/private-project")
    session.sensitive_read_approved = True
    session.evidence = [
        Evidence(
            evidence_id="ev-sensitive-001",
            source_path=".env",
            tool="read_file",
            snippet="API_KEY=report-secret-sentinel",
        ),
        Evidence(
            evidence_id="ev-source-001",
            source_path="src/main.py",
            tool="read_file",
            snippet="def main(): pass",
        ),
    ]
    session.findings = [
        Finding(
            finding_id="fnd-secret-001",
            title="API key report-secret-sentinel expuesta",
            explanation="Valor API_KEY=report-secret-sentinel encontrado.",
            severity=Severity.HIGH,
            status=FindingStatus.CONFIRMED,
            origin=FindingOrigin.DETERMINISTIC,
            evidence_ids=("ev-sensitive-001",),
        ),
        Finding(
            finding_id="fnd-source-001",
            title="Función observada",
            explanation="La función existe en el código revisado.",
            severity=Severity.INFO,
            status=FindingStatus.CONFIRMED,
            origin=FindingOrigin.DETERMINISTIC,
            evidence_ids=("ev-source-001",),
        ),
    ]

    report = build_session_readme(session)

    assert "private-project" in report
    assert "C:/Users/person" not in report
    assert "report-secret-sentinel" not in report
    assert "API_KEY=" not in report
    assert "Función observada" in report
    assert "ev-source-001" in report
    assert "ev-sensitive-001" not in report
