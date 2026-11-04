"use strict";

var state = null;
var activityStatus = null;
var pendingTurn = null;
var autoApprovalScheduled = false;
var autoApprovalBlockedAction = null;
var selectedModel = "local";
var modelChoiceInitialized = false;
var desktopControlsReady = false;
var selectedProjectFolder = null;
var $ = function (id) { return document.getElementById(id); };
var workflowStages = [
  {id:"list", label:"Seleccionar alcance"},
  {id:"checks", label:"Comprobaciones locales"},
  {id:"security", label:"Análisis de seguridad"},
  {id:"analysis", label:"Investigación de causa"},
  {id:"sources", label:"Procedencia de fuentes"},
  {id:"docs", label:"Informe técnico"},
  {id:"patches", label:"Recomendaciones"},
  {id:"review", label:"Revisión final"}
];

function element(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined) { node.textContent = text; }
  return node;
}

function appendInlineMarkdown(host, text) {
  var pattern = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*]+\*)/g;
  var cursor = 0;
  var match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      host.appendChild(document.createTextNode(text.slice(cursor, match.index)));
    }
    var token = match[0];
    var node;
    if (token.slice(0, 2) === "**" || token.slice(0, 2) === "__") {
      node = element("strong", "", token.slice(2, -2));
    } else if (token.charAt(0) === "`") {
      node = element("code", "", token.slice(1, -1));
    } else {
      node = element("em", "", token.slice(1, -1));
    }
    host.appendChild(node);
    cursor = match.index + token.length;
  }
  if (cursor < text.length) {
    host.appendChild(document.createTextNode(text.slice(cursor)));
  }
}

function renderFormattedMessage(host, text) {
  host.replaceChildren();
  var lines = String(text).replace(/\r/g, "").split("\n");
  var paragraph = [];
  var list = null;
  var codeLines = null;

  function flushParagraph() {
    if (!paragraph.length) { return; }
    var node = element("p");
    appendInlineMarkdown(node, paragraph.join(" "));
    host.appendChild(node);
    paragraph = [];
  }

  function flushList() {
    if (list) { host.appendChild(list); list = null; }
  }

  lines.forEach(function (line) {
    if (line.trim().indexOf("```") === 0) {
      flushParagraph();
      flushList();
      if (codeLines === null) {
        codeLines = [];
      } else {
        var pre = element("pre");
        pre.appendChild(element("code", "", codeLines.join("\n")));
        host.appendChild(pre);
        codeLines = null;
      }
      return;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      return;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      return;
    }
    var heading = line.match(/^\s{0,3}(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      var headingNode = element("h" + (heading[1].length + 1));
      appendInlineMarkdown(headingNode, heading[2]);
      host.appendChild(headingNode);
      return;
    }
    var item = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
    if (item) {
      flushParagraph();
      var ordered = !!item[2];
      if (!list || list.tagName.toLowerCase() !== (ordered ? "ol" : "ul")) {
        flushList();
        list = element(ordered ? "ol" : "ul");
      }
      var listItem = element("li");
      appendInlineMarkdown(listItem, item[3]);
      list.appendChild(listItem);
      return;
    }
    flushList();
    paragraph.push(line.trim());
  });
  flushParagraph();
  flushList();
  if (codeLines !== null) {
    var unfinished = element("pre");
    unfinished.appendChild(element("code", "", codeLines.join("\n")));
    host.appendChild(unfinished);
  }
}

function showToast(text) {
  var toast = $("toast");
  toast.textContent = text;
  toast.classList.add("show");
  window.setTimeout(function () { toast.classList.remove("show"); }, 2600);
}

async function request(url, options) {
  var response;
  try {
    response = await fetch(url, options || {});
  } catch (error) {
    showToast("No se pudo conectar con AudiAI. Comprueba que el servidor siga activo.");
    return null;
  }
  if (!response.ok) {
    var detail = "";
    try { detail = (await response.json()).detail || ""; } catch (error) { /* sin detalle JSON */ }
    showToast("Error " + response.status + (detail ? ": " + detail : ""));
    return null;
  }
  return response.json();
}

function post(url, body) {
  return request(url, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body || {})
  });
}

async function postChatStream(url, body, onDelta) {
  var response;
  try {
    response = await fetch(url, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body || {})
    });
  } catch (error) {
    showToast("No se pudo conectar con AudiAI. Comprueba que el servidor siga activo.");
    return null;
  }
  if (!response.ok) {
    var detail = "";
    try { detail = (await response.json()).detail || ""; } catch (error) { detail = ""; }
    showToast("Error " + response.status + (detail ? ": " + detail : ""));
    return null;
  }
  if (!response.body || !response.body.getReader) {
    showToast("Este navegador no permite recibir la respuesta en directo.");
    return null;
  }

  var reader = response.body.getReader();
  var decoder = new TextDecoder();
  var buffer = "";
  var finalState = null;
  var streamError = null;

  function consume(block) {
    var eventName = "message";
    var data = [];
    block.split(/\r?\n/).forEach(function (line) {
      if (line.indexOf("event:") === 0) { eventName = line.slice(6).trim(); }
      if (line.indexOf("data:") === 0) { data.push(line.slice(5).trim()); }
    });
    if (!data.length) { return; }
    var payload;
    try { payload = JSON.parse(data.join("\n")); }
    catch (error) {
      streamError = "AudiAI envió una respuesta de chat inválida.";
      return;
    }
    if (eventName === "delta" && typeof payload.text === "string") {
      onDelta(payload.text);
    } else if (eventName === "state") {
      finalState = payload;
    } else if (eventName === "error") {
      streamError = "Error " + payload.status + ": " + payload.detail;
    }
  }

  try {
    while (true) {
      var part = await reader.read();
      buffer += decoder.decode(part.value || new Uint8Array(), {stream:!part.done});
      var separator;
      while ((separator = buffer.indexOf("\n\n")) !== -1) {
        consume(buffer.slice(0, separator));
        buffer = buffer.slice(separator + 2);
      }
      if (part.done) { break; }
    }
    if (buffer.trim()) { consume(buffer); }
  } catch (error) {
    streamError = "La conexión se interrumpió mientras AudiAI respondía.";
  }
  if (streamError) {
    showToast(streamError);
    return null;
  }
  if (!finalState) {
    showToast("La respuesta terminó antes de sincronizar la conversación.");
  }
  return finalState;
}

function stageIndex(stageId) {
  return activeWorkflowStages().findIndex(function (stage) {
    return stage.id === stageId;
  });
}

function activeWorkflowStages() {
  return state && state.agent_mode === "agent"
    ? workflowStages.filter(function (stage) {
      return ["list", "checks", "analysis"].indexOf(stage.id) !== -1;
    })
    : workflowStages;
}

function failedStageIndex() {
  var failure = state.ready_items.find(function (item) {
    return item.kind === "agent_failure" ||
      item.title.toLowerCase().indexOf("no disponible") !== -1;
  });
  if (!failure) { return -1; }
  var title = failure.title.toLowerCase();
  if (title.indexOf("seguridad") !== -1) { return stageIndex("security"); }
  if (title.indexOf("fuentes") !== -1) { return stageIndex("sources"); }
  if (title.indexOf("document") !== -1) { return stageIndex("docs"); }
  if (title.indexOf("parche") !== -1) { return stageIndex("patches"); }
  if (title.indexOf("revisor") !== -1) { return stageIndex("review"); }
  return stageIndex("analysis");
}

function completedStageCount() {
  var currentIndex = state.pending_action
    ? stageIndex(state.pending_action.kind)
    : state.state === "completa" ? activeWorkflowStages().length : -1;
  var failedIndex = failedStageIndex();
  return Math.max(
    0,
    currentIndex - (failedIndex >= 0 && failedIndex < currentIndex ? 1 : 0)
  );
}

function renderSummaryMetrics() {
  var completed = completedStageCount();
  var metrics = [
    {value:state.findings_count, label:"Hallazgos"},
    {value:state.evidence_count, label:"Evidencias"},
    {value:completed + "/" + activeWorkflowStages().length, label:"Etapas listas"}
  ];
  var host = $("summaryMetrics");
  host.replaceChildren();
  metrics.forEach(function (metric, index) {
    var item = element(index === 1 ? "button" : "div", "summary-metric" +
      (index === 1 ? " metric-button" : ""));
    if (index === 1) {
      item.type = "button";
      item.setAttribute("aria-label", "Ver las " + state.evidence_count + " evidencias");
      item.addEventListener("click", function () { togglePanel(true); setPanelTab("evidence"); });
    }
    item.appendChild(element("strong", "", String(metric.value)));
    item.appendChild(element("span", "", metric.label));
    host.appendChild(item);
  });
}

function render() {
  if (!state) { return; }
  renderModelStatus();
  if (state.agent_mode) {
    $("agentModeSelector").value = state.agent_mode;
    $("agentModeChip").textContent =
      $("agentModeSelector").options[$("agentModeSelector").selectedIndex].text;
  }
  var awaitingProject = state.state === "esperando_ruta";
  $("agentModeSelector").disabled = !awaitingProject;
  $("analysisModeSelector").disabled = !awaitingProject;
  $("contextBtn").disabled = state.state !== "esperando_ruta";
  updateSelectorHints();
  var readyCount = state.ready_items.length;
  $("panelCount").textContent = String(readyCount);
  $("panelCount").hidden = readyCount === 0;
  $("newSessionBtn").disabled = !!activityStatus;
  $("welcome").hidden = !!pendingTurn || state.messages.some(function (message) {
    return message.author === "user";
  });
  $("downloadReportBtn").hidden = state.state !== "completa";
  renderSummaryMetrics();

  var log = $("chatLog");
  var activityWasOpen = !!$("activityDisclosure") && $("activityDisclosure").open;
  log.replaceChildren();
  var auditInProgress = !!state.pending_action || state.ready_items.length > 0 ||
    state.state === "completa" || state.state === "bloqueada";
  var latestAgentMessage = -1;
  if (state.state === "completa" || state.state === "bloqueada") {
    for (var messageIndex = state.messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
      if (state.messages[messageIndex].author === "agent") {
        latestAgentMessage = messageIndex;
        break;
      }
    }
  }
  state.messages.forEach(function (message, index) {
    if (message.author === "agent" && index === 0) { return; }
    if (auditInProgress && message.author === "agent" &&
        (latestAgentMessage < 0 || index !== latestAgentMessage)) { return; }
    var bubble = element("article", "msg " + message.author);
    bubble.setAttribute(
      "aria-label",
      message.author === "agent" ? "Respuesta de AudiAI" : "Tu mensaje"
    );
    var copy = element("div", "message-copy");
    renderFormattedMessage(copy, message.text);
    bubble.appendChild(copy);
    log.appendChild(bubble);
  });

  if (pendingTurn) {
    var userBubble = element("article", "msg user");
    userBubble.setAttribute("aria-label", "Tu mensaje");
    var userCopy = element("div", "message-copy");
    renderFormattedMessage(userCopy, pendingTurn.text);
    userBubble.appendChild(userCopy);
    log.appendChild(userBubble);
  }

  renderActivityDisclosure(log, activityWasOpen);
  if (!activityStatus && state.pending_action) {
    var pending = state.pending_action;
    var card = element("div", "action-card");
    var pendingIndex = stageIndex(pending.kind);
    var sensitive = pending.kind === "sensitive";
    var externalConsent = (pending.kind === "security" ||
      (pending.kind === "analysis" && state.agent_mode === "agent")) &&
      state.roles_provider === "strands" && !state.external_data_consent;
    card.appendChild(element("div", "action-eyebrow",
      sensitive ? "PERMISO PARA ARCHIVOS CONFIDENCIALES" :
      externalConsent ? "PERMISO PARA USAR EL MODELO CONECTADO" :
      "ETAPA " + (pendingIndex + 1) + " DE " + activeWorkflowStages().length));
    card.appendChild(element("div", "caption", pending.caption));
    card.appendChild(element("div", "hint", pending.hint));
    if (sensitive || externalConsent) {
      var disclosure = sensitive
        ? "Solo se abrirán los archivos enumerados arriba. No se modificarán."
        : "La evidencia y el contexto autorizado se enviarán al proveedor externo indicado abajo. " +
          (state.sensitive_read_approved
            ? "Esto incluye los archivos confidenciales que autorizaste leer. "
            : "") +
          "No se enviará nada hasta que lo autorices.";
      card.appendChild(element("div", "approval-note", disclosure));
    }
    var approve = element(
      "button",
      "btn primary",
      sensitive ? "Autorizar lectura" :
      externalConsent ? "Autorizar IA y continuar" :
      autoApprovalBlockedAction === pending.action_id ? "Reintentar" :
      "Continuar"
    );
    approve.type = "button";
    approve.dataset.actionId = pending.action_id;
    approve.addEventListener("click", onApprove);
    card.appendChild(approve);
    if (sensitive || externalConsent) {
      var skip = element("button", "btn secondary",
        sensitive ? "Omitir archivos" : "Continuar sin IA externa");
      skip.type = "button";
      skip.dataset.actionId = pending.action_id;
      skip.addEventListener("click", onSkip);
      card.appendChild(skip);
    }
    log.appendChild(card);
  }
  $("chatArea").scrollTop = $("chatArea").scrollHeight;

  renderReady();
  renderDocs();
  renderPatches();
  renderEvidence();
  var input = $("messageInput");
  input.disabled = !!state.pending_action || !!activityStatus || !!pendingTurn ||
    state.state === "completa" || state.state === "bloqueada";
  input.placeholder = state.state === "esperando_ruta"
    ? activityStatus
      ? activityStatus.title + "…"
      : "Escribe una instrucción o pregunta"
    : state.pending_action
      ? activityStatus
        ? activityStatus.title + "…"
        : "Waiting for approval…"
      : "La auditoría terminó";
  var isWorking = !!activityStatus || !!pendingTurn;
  $("sendBtn").disabled = state.state === "completa" ||
    state.state === "bloqueada" ||
    (!isWorking && !!state.pending_action);
  $("sendBtn").classList.toggle("is-working", isWorking);
  $("sendBtn").setAttribute("aria-busy", String(isWorking));
  $("sendBtn").setAttribute(
    "aria-label",
    isWorking ? "Mostrar las acciones en curso" : "Enviar mensaje"
  );
  $("sendBtn").title = isWorking
    ? "Mostrar las acciones en curso"
    : "Enviar mensaje";
  renderContextAttachment();
  scheduleAutomaticApproval();
}

function renderContextAttachment() {
  var attachment = $("contextAttachment");
  var removeButton = $("removeContextBtn");
  attachment.hidden = !selectedProjectFolder;
  if (!selectedProjectFolder) { return; }
  var folderName = selectedProjectFolder.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  $("contextAttachmentLabel").textContent = "Proyecto · " + (folderName || selectedProjectFolder);
  attachment.title = selectedProjectFolder;
  removeButton.disabled = state.state !== "esperando_ruta" || !!pendingTurn || !!activityStatus;
}

function renderActivityDisclosure(host, wasOpen) {
  var entries = state.ready_items || [];
  var agentNotes = state.messages.filter(function (message, index) {
    return message.author === "agent" && message.text &&
      index > 0;
  });
  if (!entries.length && !activityStatus) { return; }

  var disclosure = element("details", "activity-disclosure");
  disclosure.id = "activityDisclosure";
  disclosure.open = wasOpen;
  var summary = element("summary", "activity-summary");
  if (activityStatus) {
    summary.appendChild(element("span", "activity-spinner", ""));
    summary.appendChild(element("span", "activity-title", activityStatus.title));
  } else {
    summary.appendChild(element(
      "span",
      "activity-title",
      "Actividad · " + entries.length + " etapas"
    ));
  }
  disclosure.appendChild(summary);
  disclosure.setAttribute("aria-busy", String(!!activityStatus));
  var list = element("div", "activity-list");
  if (activityStatus) {
    var current = element("div", "activity-entry");
    current.setAttribute("role", "status");
    current.setAttribute("aria-live", "polite");
    current.appendChild(element("strong", "", activityStatus.title));
    current.appendChild(element("span", "", activityStatus.detail));
    list.appendChild(current);
  }
  entries.forEach(function (item) {
    var entry = element("div", "activity-entry");
    entry.appendChild(element("strong", "", item.title));
    entry.appendChild(element("span", "", item.detail + " · " + item.origin));
    list.appendChild(entry);
  });
  if (agentNotes.length) {
    var notes = element("details", "activity-notes");
    notes.appendChild(element("summary", "", "Actualizaciones del proceso"));
    var noteList = element("ul", "");
    agentNotes.forEach(function (message) {
      noteList.appendChild(element("li", "", message.text));
    });
    notes.appendChild(noteList);
    list.appendChild(notes);
  }
  list.appendChild(element(
    "span",
    "activity-footnote",
    "Registro de acciones y resúmenes; no incluye razonamiento interno privado del modelo."
  ));
  disclosure.appendChild(list);
  host.appendChild(disclosure);
}

function setWelcomeName(name) {
  var hour = new Date().getHours();
  var greeting = hour < 12 ? "Buenos días" : hour < 18 ? "Buenas tardes" : "Buenas noches";
  var cleanName = typeof name === "string" ? name.trim() : "";
  $("welcomeGreeting").textContent = cleanName
    ? greeting + ", " + cleanName
    : greeting;
}

function renderModelStatus() {
  var configured = state.roles_provider === "strands";
  var localModel = state.roles_provider === "lmstudio";
  var reportedModel = state.chat_model || state.analysis_model;
  var selector = $("modelSelector");
  var nextSelection = modelChoiceInitialized
    ? selector.value || selectedModel
    : "local";
  selector.replaceChildren();
  selector.appendChild(new Option(
    localModel ? "LM Studio · local" : "Auto",
    "local"
  ));
  if (configured) {
    selector.appendChild(new Option("OpenRouter", "external"));
  }
  selectedModel = configured && nextSelection === "external" ? "external" : "local";
  modelChoiceInitialized = true;
  selector.value = selectedModel;
  selector.disabled = !!state.external_data_consent;
  var label = reportedModel
    ? formatModelName(reportedModel)
    : selectedModel === "external" ? "OpenRouter" : "Auto";
  $("modelChip").textContent = label;
  updateSelectorHints();
}

function formatModelName(modelId) {
  var name = modelId.split("/").pop();
  return name.replace(/^qwen(?=\d)/i, "Qwen ");
}

function updateSelectorHints() {
  var analysisHints = {
    reading: "Lectura: describe el código y los hechos observables.",
    research: "Investigación: relaciona causas con evidencia; sin búsqueda web.",
    unspecified: "Sin especificar: el agente elige el enfoque."
  };
  var agentHints = {
    agent: "Agente: un análisis; informe y recomendaciones locales.",
    multiagent: "Multiagente: especialistas coordinados por etapas."
  };
  $("analysisModeHint").textContent =
    analysisHints[$("analysisModeSelector").value];
  $("agentModeHint").textContent =
    agentHints[$("agentModeSelector").value];
  $("modelDescription").textContent = selectedModel === "external"
    ? "OpenRouter comparte evidencia solo después de tu autorización."
    : state.roles_provider === "lmstudio"
      ? "Modelo local: las solicitudes se envían a LM Studio en este equipo."
      : state.roles_provider === "strands"
      ? "Auto evita el envío externo; la auditoría continúa localmente."
      : "Auto usa el modo simulado; no hay proveedor conectado.";
}

function renderReady() {
  var host = $("readyList");
  host.replaceChildren();
  if (!state.ready_items.length) {
    host.appendChild(element("p", "empty", "El resumen de cada especialista aparecerá aquí al completar su etapa."));
    return;
  }
  state.ready_items.forEach(function (item) {
    var box = element("article", "ready-item");
    var header = element("div", "ready-header");
    header.appendChild(element("span", "ready-icon", readyIcon(item.kind)));
    var title = element("div", "ready-title");
    title.appendChild(element("span", "ready-role", readyRole(item.kind)));
    title.appendChild(element("strong", "", item.title));
    header.appendChild(title);
    box.appendChild(header);
    box.appendChild(element("p", "", item.detail));
    box.appendChild(element("span", "origin-badge", item.origin));
    host.appendChild(box);
  });
}

function readyRole(kind) {
  var labels = {
    checks:"Comprobaciones locales", security:"Agente de seguridad",
    analysis:"Agente investigador", sources:"Investigación de fuentes",
    docs:"Agente de documentación", patches:"Agente de remediación",
    review:"Revisión independiente", agent_failure:"Estado del agente"
  };
  return labels[kind] || "Actividad de auditoría";
}

function readyIcon(kind) {
  var icons = {
    checks:"✓", security:"S", analysis:"C", sources:"F",
    docs:"D", patches:"P", review:"R", agent_failure:"!"
  };
  return icons[kind] || "•";
}

function renderDocs() {
  var host = $("docsList");
  host.replaceChildren();
  if (!state.docs.length) {
    host.appendChild(element("p", "empty", "Aún no hay documentos."));
    return;
  }
  state.docs.forEach(function (section, index) {
    var details = document.createElement("details");
    details.className = "doc-section";
    details.open = index === 0;
    details.appendChild(element("summary", "", section.title));
    details.appendChild(element("div", "body", section.body));
    if (section.evidence_ids && section.evidence_ids.length) {
      details.appendChild(element("span", "origin", "Evidencia: " + section.evidence_ids.join(", ")));
    }
    details.appendChild(element("span", "origin", section.origin));
    host.appendChild(details);
  });
}

function renderPatches() {
  var host = $("patchList");
  host.replaceChildren();
  if (!state.patches.length) {
    host.appendChild(element("p", "empty", "Aún no hay parches."));
    return;
  }
  state.patches.forEach(function (patch) {
    var card = element("article", "patch-card");
    card.appendChild(element("strong", "", patch.title));
    card.appendChild(element("p", "", patch.description));
    if (patch.evidence_ids && patch.evidence_ids.length) {
      card.appendChild(element("span", "origin", "Evidencia: " + patch.evidence_ids.join(", ")));
    }
    card.appendChild(element("span", "origin", patch.origin));
    if (patch.diff) { card.appendChild(element("pre", "", patch.diff)); }
    host.appendChild(card);
  });
}

function renderEvidence() {
  var host = $("evidenceList");
  host.replaceChildren();
  if (!state.evidence.length) {
    host.appendChild(element("p", "empty", "Aún no hay evidencias."));
    return;
  }
  state.evidence.forEach(function (item) {
    var details = document.createElement("details");
    details.className = "evidence-card";
    var summary = document.createElement("summary");
    summary.appendChild(element("span", "evidence-marker", "E"));
    var heading = element("span", "evidence-heading");
    heading.appendChild(element(
      "strong",
      "",
      item.source_path + (item.line_start
        ? ":" + item.line_start + (item.line_end && item.line_end !== item.line_start
          ? "-" + item.line_end : "")
        : "")
    ));
    heading.appendChild(element("span", "", item.tool + " · " + item.evidence_id));
    summary.appendChild(heading);
    details.appendChild(summary);
    var body = element("div", "evidence-body");
    body.appendChild(element("pre", "", item.snippet));
    (item.limitations || []).forEach(function (limitation) {
      body.appendChild(element("p", "evidence-limit", limitation));
    });
    details.appendChild(body);
    host.appendChild(details);
  });
  $("evidenceHeading").textContent =
    "Evidencia recopilada · " + state.evidence.length;
}

function setPanelTab(tabName) {
  var selected = {
    review:tabName === "review",
    evidence:tabName === "evidence",
    patches:tabName === "patches"
  };
  ["review", "evidence", "patches"].forEach(function (name) {
    var tab = $(name === "patches" ? "patchTab" : name + "Tab");
    var body = $(name === "patches" ? "patchBody" : name + "Body");
    tab.classList.toggle("active", selected[name]);
    tab.setAttribute("aria-selected", String(selected[name]));
    body.hidden = !selected[name];
  });
}

function needsUserApproval(action) {
  if (action.kind === "sensitive") {
    return $("sensitivePolicy").value !== "exclude";
  }
  return (action.kind === "security" ||
    (action.kind === "analysis" && $("agentModeSelector").value === "agent")) &&
    state.roles_provider === "strands" && selectedModel === "external" &&
    !state.external_data_consent;
}

function shouldSkipAutomatically(action) {
  if (action.kind === "sensitive") {
    return $("sensitivePolicy").value === "exclude";
  }
  return (action.kind === "security" ||
    (action.kind === "analysis" && $("agentModeSelector").value === "agent")) &&
    state.roles_provider === "strands" && selectedModel !== "external";
}

function scheduleAutomaticApproval() {
  var action = state && state.pending_action;
  if (
    !action || activityStatus || needsUserApproval(action) ||
    autoApprovalBlockedAction === action.action_id || autoApprovalScheduled
  ) { return; }
  autoApprovalScheduled = true;
  window.setTimeout(function () {
    autoApprovalScheduled = false;
    if (state && state.pending_action &&
        state.pending_action.action_id === action.action_id) {
      if (shouldSkipAutomatically(action)) { onSkip(null, true); }
      else { onApprove(null, true); }
    }
  }, 0);
}

async function onApprove(event, automatic) {
  if (!state || !state.pending_action || activityStatus) { return; }
  var pending = state.pending_action;
  autoApprovalBlockedAction = null;
  var activityCopy = {
    list: ["Reading files", "Leyendo archivos permitidos en modo de solo lectura."],
    sensitive: ["Reading approved files", "Leyendo únicamente los archivos confidenciales autorizados."],
    checks: ["Analyzing code", "Ejecutando comprobaciones locales sobre el contenido permitido."],
    security: ["Analyzing security", "Evaluando los riesgos a partir de evidencia autorizada."],
    analysis: [
      "Analyzing causes",
      "Relacionando el problema con la evidencia autorizada."
    ],
    sources: ["Checking local sources", "Revisando procedencia y referencias locales; sin búsqueda web."],
    docs: ["Writing report", "Redactando el informe a partir de los resultados obtenidos."],
    patches: ["Preparing recommendations", "Preparando sugerencias; no se modifican archivos."],
    review: ["Reviewing findings", "Contrastando las conclusiones con la evidencia disponible."]
  }[pending.kind] || ["Processing", "Procesando esta etapa de la revisión."];
  activityStatus = {title:activityCopy[0], detail:activityCopy[1]};
  render();
  await new Promise(function (resolve) {
    window.requestAnimationFrame(function () { window.requestAnimationFrame(resolve); });
  });
  var updated = await post(
    "/agent/sessions/" + encodeURIComponent(state.session_id) +
    "/approve/" + encodeURIComponent(pending.action_id)
  );
  activityStatus = null;
  if (updated) { state = updated; render(); }
  else {
    if (automatic) { autoApprovalBlockedAction = pending.action_id; }
    await reconcileSession("La acción pudo completarse; sincronizando el estado.");
    render();
  }
}

async function onSkip(event, automatic) {
  if (!state || !state.pending_action || activityStatus) { return; }
  var pending = state.pending_action;
  activityStatus = {
    title:pending.kind === "sensitive" ? "Skipping sensitive files" : "Continuing locally",
    detail:"No se leerán archivos omitidos ni se enviará evidencia al proveedor."
  };
  render();
  var updated = await post(
    "/agent/sessions/" + encodeURIComponent(state.session_id) +
    "/skip/" + encodeURIComponent(pending.action_id)
  );
  activityStatus = null;
  if (updated) { state = updated; render(); }
  else {
    if (automatic) { autoApprovalBlockedAction = pending.action_id; }
    await reconcileSession("No se pudo completar la decisión; sincronizando.");
    render();
  }
}

async function onSend(event) {
  event.preventDefault();
  if (!state) { return; }
  if (activityStatus || pendingTurn) {
    var activity = $("activityDisclosure");
    if (activity) { activity.open = !activity.open; }
    return;
  }
  if (state.pending_action) { return; }
  var input = $("messageInput");
  var text = input.value.trim();
  if (!text) { return; }
  var attachedProject = state.state === "esperando_ruta" ? selectedProjectFolder : null;
  var startsAudit = !!attachedProject ||
    /^[A-Za-z]:[\\/]|^\\\\|^\/|^\.{1,2}[\\/]|^~[\\/]/.test(text);
  pendingTurn = {text:text, isAudit:startsAudit};
  activityStatus = startsAudit
    ? {
      title:"Reading files",
      detail:"Comprobando la carpeta y leyendo archivos permitidos."
    }
    : {
      title:"Thinking",
      detail:"Preparando respuesta."
    };
  render();
  await new Promise(function (resolve) {
    window.requestAnimationFrame(resolve);
  });
  var messageUrl = "/agent/sessions/" +
    encodeURIComponent(state.session_id) + "/messages";
  var messageBody = {
    text:text,
    analysis_mode:$("analysisModeSelector").value,
    agent_mode:$("agentModeSelector").value
  };
  if (attachedProject) { messageBody.project_path = attachedProject; }
  var updated = !startsAudit && state.state === "esperando_ruta"
    ? await postChatStream(messageUrl + "/stream", messageBody, function () {
      if (activityStatus && activityStatus.title === "Thinking") {
        activityStatus = {
          title:"Responding",
          detail:"Redactando respuesta."
        };
        render();
      }
    })
    : await post(messageUrl, messageBody);
  activityStatus = null;
  pendingTurn = null;
  if (updated) { state = updated; input.value = ""; render(); }
  else {
    render();
    await reconcileSession("Sincronizando la conversación con el servidor.");
  }
}

async function reconcileSession(message) {
  if (!state) { return; }
  var current = await request(
    "/agent/sessions/" + encodeURIComponent(state.session_id),
    {method:"GET"}
  );
  if (current) {
    state = current;
    render();
    showToast(message);
  }
}

async function newSession() {
  var created = await post("/agent/sessions");
  if (created) {
    state = created;
    selectedProjectFolder = null;
    render();
    return;
  }
  // Recover the common case where the server created the session but a local
  // proxy dropped the POST response. The server is single-user/in-memory for
  // this demo; newest chat id is the just-created session.
  var summaries = await request("/agent/sessions", {method:"GET"});
  if (!summaries || !summaries.length) { return; }
  summaries.sort(function (a, b) {
    return Number(b.session_id.replace("chat-", "")) -
      Number(a.session_id.replace("chat-", ""));
  });
  var recovered = await request(
    "/agent/sessions/" + encodeURIComponent(summaries[0].session_id),
    {method:"GET"}
  );
  if (recovered) {
    state = recovered;
    selectedProjectFolder = null;
    render();
    showToast("Sesión recuperada tras una respuesta de red interrumpida.");
  }
}

function togglePanel(force) {
  var panel = $("sidePanel");
  var open = force === undefined ? !panel.classList.contains("open") : force;
  panel.classList.toggle("open", open);
  $("app").classList.toggle("panel-open", open);
  panel.setAttribute("aria-hidden", String(!open));
  $("panelToggle").setAttribute("aria-expanded", String(open));
}

$("messageForm").addEventListener("submit", onSend);
$("analysisModeSelector").addEventListener("change", function () {
  $("analysisModeChip").textContent = this.options[this.selectedIndex].text;
  updateSelectorHints();
  $("readMenu").open = false;
});
$("agentModeSelector").addEventListener("change", function () {
  $("agentModeChip").textContent = this.options[this.selectedIndex].text;
  updateSelectorHints();
  $("agentModeMenu").open = false;
});
$("modelSelector").addEventListener("change", function () {
  selectedModel = $("modelSelector").value;
  $("modelMenu").open = false;
  render();
});
$("sensitivePolicy").addEventListener("change", function () {
  $("sensitivePolicyHint").textContent = this.value === "exclude"
    ? "Los archivos confidenciales se omiten sin preguntar."
    : "AudiAI pedirá permiso antes de leerlos.";
  $("readMenu").open = false;
  render();
});
$("newSessionBtn").addEventListener("click", newSession);
$("panelToggle").addEventListener("click", function () { togglePanel(); });
$("closePanel").addEventListener("click", function () { togglePanel(false); });
document.querySelectorAll("[data-panel-tab]").forEach(function (tab) {
  tab.addEventListener("click", function () { setPanelTab(tab.dataset.panelTab); });
});
document.addEventListener("pointerdown", function (event) {
  if (event.target.closest(".selector-menu")) {
    document.querySelectorAll(".selector-menu[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) { menu.open = false; }
    });
    return;
  }
  document.querySelectorAll(".selector-menu[open]").forEach(function (menu) {
    menu.open = false;
  });
});
document.addEventListener("keydown", function (event) {
  if (event.key === "Escape") {
    var openMenus = document.querySelectorAll(".selector-menu[open]");
    if (openMenus.length) {
      openMenus.forEach(function (menu) { menu.open = false; });
      event.stopPropagation();
      return;
    }
    togglePanel(false);
  }
});
$("messageInput").addEventListener("keydown", function (event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("messageForm").requestSubmit();
  }
});

function enableDesktopContextPicker() {
  if (!desktopControlsReady && window.pywebview && window.pywebview.api) {
    desktopControlsReady = true;
    $("contextBtn").hidden = false;
    window.pywebview.api.get_display_name().then(setWelcomeName).catch(function () {
      setWelcomeName("");
      showToast("No se pudo cargar el nombre de usuario del escritorio.");
    });
  }
}

window.addEventListener("pywebviewready", enableDesktopContextPicker);
if (window.pywebview && window.pywebview.api) { enableDesktopContextPicker(); }
$("contextBtn").addEventListener("click", async function () {
  var button = $("contextBtn");
  button.disabled = true;
  try {
    var folder = await window.pywebview.api.select_project_folder();
    if (folder) {
      selectedProjectFolder = folder;
      renderContextAttachment();
    }
  } catch (error) {
    showToast("No se pudo abrir el selector de carpetas.");
  } finally {
    button.disabled = !!(state && state.state !== "esperando_ruta");
  }
});

$("downloadReportBtn").addEventListener("click", async function () {
  var button = $("downloadReportBtn");
  button.disabled = true;
  try {
    var response = await fetch(
      "/agent/sessions/" + encodeURIComponent(state.session_id) + "/report.md"
    );
    if (!response.ok) {
      throw new Error("No se pudo descargar el informe (" + response.status + ").");
    }
    var downloadUrl = URL.createObjectURL(await response.blob());
    var link = document.createElement("a");
    link.href = downloadUrl;
    link.download = "AudiAI-" + state.session_id + "-README.md";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(downloadUrl);
  } catch (error) {
    showToast(error.message || "No se pudo descargar el informe.");
  } finally {
    button.disabled = false;
  }
});

$("removeContextBtn").addEventListener("click", function () {
  if (state && state.state === "esperando_ruta" && !pendingTurn && !activityStatus) {
    selectedProjectFolder = null;
    renderContextAttachment();
  }
});

setWelcomeName("");
newSession();
