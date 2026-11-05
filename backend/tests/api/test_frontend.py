"""Fases 8 y 11: interfaz y recursos locales sin romper los endpoints existentes.

La home es el chat del agente (Fase 11); la interfaz clásica de una sola
auditoría vive en ``/classic``. Estas pruebas verifican los documentos y
recursos locales servidos, no el navegador.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import create_app


def _served(path: str) -> str:
    with TestClient(create_app()) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        return response.text


def _served_chat_bundle() -> str:
    with TestClient(create_app()) as client:
        html = client.get("/")
        assert html.status_code == 200
        css = client.get("/assets/audiai.css")
        javascript = client.get("/assets/audiai.js")
        assert css.status_code == 200
        assert javascript.status_code == 200
        return "\n".join((html.text, css.text, javascript.text))


def test_home_is_the_chat_and_declares_the_provider_visibly() -> None:
    html = _served_chat_bundle()

    # Documento local, sin estilos ni scripts embebidos.
    assert "<title>AudiAI</title>" in html
    assert 'href="/assets/audiai.css"' in html
    assert 'src="/assets/audiai.js"' in html
    assert "<style>" not in _served("/")
    assert "<script>" not in _served("/")
    assert 'message.author === "agent" ? "AudiAI" : "Tú"' not in html

    # Pantalla principal: saludo y composer.
    assert "<h1>AudiAI</h1>" in html
    assert 'id="welcomeGreeting"' in html
    assert "¿Qué quieres resolver hoy?" in html
    assert "Buenas noches" in html
    assert 'id="welcomeCapability"' not in html
    assert 'class="welcome-trust"' not in html
    assert 'placeholder="Escribe una instrucción o pregunta"' in html
    assert 'placeholder="Escribe una instrucción o pregunta" disabled' in html
    assert 'id="contextBtn"' in html
    assert 'id="contextAttachment"' in html
    assert 'id="contextAttachmentLabel"' in html
    assert 'id="removeContextBtn"' in html
    assert 'aria-label="Elegir carpeta del proyecto; se analizará localmente sin subir archivos"' in html
    assert "análisis local, sin subir archivos" in html
    assert "select_project_folder" in html
    assert 'messageBody.project_path = attachedProject' in html
    assert 'selectedProjectFolder = folder' in html

    # Proveedor y modelo visibles, con atribución honesta.
    assert "function formatModelName(modelId)" in html
    assert 'option value="local">Auto</option>' in html
    assert 'localModel ? "LM Studio · local" : "Auto"' in html
    assert 'state.roles_provider === "lmstudio"' in html
    assert "Modelo local: las solicitudes se envían a LM Studio en este equipo." in html
    assert 'id="modelMenu"' in html
    assert 'id="modelChip"' in html
    assert 'id="modelSelector"' in html
    assert 'id="connectionDot"' not in html
    assert "simulado" in html.lower()

    # Selectores de alcance y orquestación.
    assert 'id="readMenu"' in html
    assert ">Lectura</span>" in html
    assert 'id="analysisModeSelector"' in html
    assert 'aria-describedby="analysisModeHint"' in html
    assert 'class="selector-hint" id="analysisModeHint"' in html
    assert 'value="research">Investigación</option>' in html
    assert 'id="sensitivePolicy"' in html
    assert 'id="agentModeMenu"' in html
    assert 'value="agent">Agente</option>' in html
    assert 'value="multiagent">Multiagente</option>' in html
    assert 'aria-describedby="agentModeHint"' in html
    assert 'class="selector-hint" id="agentModeHint"' in html
    assert "agente: un análisis" in html.lower()
    assert "especialistas coordinados" in html.lower()
    assert "sin búsqueda web" in html.lower()
    assert "pointerdown" in html
    assert 'event.key === "Escape"' in html

    # Traza de progreso honesta: estados reales, resumen expandible y nota
    # que aclara que no se expone razonamiento privado.
    assert "function renderWorkBlock(turn)" in html
    assert 'element("details", "activity-disclosure")' in html
    assert 'element("span", "activity-spinner", "")' in html
    assert 'element("span", "activity-check", "✓")' in html
    assert "Trabajó durante " in html
    assert "Esperando tu autorización…" in html
    assert "Waiting for approval…" not in html
    assert "Al pedir permiso" in html
    assert "activity-step" in html
    assert ".activity-disclosure[aria-busy=\"true\"] > summary" in html
    assert ".activity-spinner {" in html
    assert "razonamiento interno privado" in html
    assert "Registro de acciones reales" in html
    assert "function lastActivity(session, kind)" in html
    assert "function formatDuration(milliseconds)" in html
    assert "duration_ms: activity.duration_ms" in html
    assert "MIN_STEP_MS" in html
    assert "delete openActivities[liveWork.turn]" in html
    assert 'title:"Pensando"' in html or "label:\"Pensando\"" in html
    assert 'security: ["Analyzing security"' not in html
    assert "Actualizaciones del proceso" not in html
    assert "updateStreamingMessage" not in html

    # Envío con bloqueo de UI y respuestas en directo cuando aplica.
    assert 'classList.toggle("is-working", working)' in html
    assert 'setAttribute("aria-busy", String(working))' in html
    assert ".send.is-working::before" in html
    assert "animation: send-spinner 0.8s linear infinite" in html
    assert ".send.is-working svg { visibility: hidden; }" in html
    assert "function postChatStream" in html
    assert 'analysis_mode:$("analysisModeSelector").value' in html
    assert 'agent_mode:$("agentModeSelector").value' in html
    assert "scheduleAutomaticApproval" in html
    assert "{quiet:true}" in html

    # Panel de resultados con pestañas separadas.
    assert 'id="summaryMetrics"' in html
    assert 'id="chatLog"' in html
    assert 'id="evidenceTab"' in html
    assert 'id="patchTab"' in html
    assert 'id="evidenceList"' in html
    assert "function renderEvidence()" in html
    assert "function renderPatches()" in html
    assert "/skip/" in html
    assert 'id="workflow"' not in html
    assert "PROCESO DE AUDITORÍA" not in html
    assert not any(symbol in html for symbol in ("✦", "✧", "★", "☆"))


def test_home_keeps_high_contrast_breakpoints_and_accessible_actions() -> None:
    html = _served_chat_bundle()

    assert "RESPONSIVE" not in html
    assert "@media (max-width: 720px)" in html
    assert "@media (min-width: 1120px)" in html
    assert "grid-template-columns: 64px minmax(0, 1fr)" in html
    assert "Autorizar lectura" in html
    assert "Autorizar IA y continuar" in html
    assert "prefers-reduced-motion" in html
    assert 'aria-live="polite"' in html
    assert 'aria-label="Mensaje"' in html
    assert 'aria-label="Enviar mensaje"' in html


def test_classic_interface_keeps_export_and_the_phase_7_api() -> None:
    html = _served("/classic")

    assert "Exportar JSON" in html
    # la UI clásica no decide nada: solo llama a los endpoints ya existentes
    assert '"/audits"' in html
    assert '"/audits/"' in html and '"/report"' in html


def test_neither_interface_uses_external_resources() -> None:
    for path in ("/", "/classic"):
        html = _served(path)
        script_sources = re.findall(r'<script[^>]+src="([^"]+)"', html)
        assert all(source.startswith("/") for source in script_sources)
        assert not re.search(r"<link[^>]+https?://", html)


def test_chat_styles_and_scripts_are_served_as_local_assets() -> None:
    with TestClient(create_app()) as client:
        css = client.get("/assets/audiai.css")
        javascript = client.get("/assets/audiai.js")

    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert ".msg.agent .who" not in css.text
    assert ".welcome[hidden] { display: none; }" in css.text
    assert ".activity-disclosure" in css.text
    assert ".activity-step[data-status=\"running\"]" in css.text
    assert javascript.status_code == 200
    assert "function renderFormattedMessage" in javascript.text
    assert "function postChatStream" in javascript.text
    assert "function renderWorkBlock" in javascript.text


def test_frontend_does_not_shadow_the_api() -> None:
    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        unknown_report = client.get("/audits/xxx/report")
        assert unknown_report.status_code == 404
        unknown_session = client.get("/agent/sessions/chat-9999")
        assert unknown_session.status_code == 404
