"use strict";

var state = null;
var pendingTurn = null;
var liveWork = null;
var turnDurations = {};
var openActivities = {};
var autoApprovalScheduled = false;
var autoApprovalBlockedAction = null;
var selectedModel = "local";
var modelChoiceInitialized = false;
var desktopControlsReady = false;
var selectedProjectFolder = null;
var MIN_STEP_MS = 520;
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

var STEP_COPY = {
  think: {
    label:"Pensando",
    detail:"Interpretando la solicitud y preparando el siguiente paso."
  },
  list: {
    label:"Leyendo el proyecto",
    detail:"Abriendo archivos permitidos en modo de solo lectura."
  },
  sensitive: {
    label:"Leyendo archivos autorizados",
    detail:"Accediendo únicamente a los archivos que autorizaste."
  },
  checks: {
    label:"Revisando el código",
    detail:"Ejecutando comprobaciones deterministas sobre el contenido permitido."
  },
  security: {
    label:"Evaluando seguridad",
    detail:"Relacionando la evidencia autorizada con riesgos y controles."
  },
  analysis: {
    label:"Analizando causas",
    detail:"Investigando la causa probable con la evidencia disponible."
  },
  sources: {
    label:"Comprobando fuentes",
    detail:"Revisando la procedencia local; sin búsqueda web."
  },
  docs: {
    label:"Documentando",
    detail:"Redactando el informe a partir de los resultados obtenidos."
  },
  patches: {
    label:"Preparando recomendaciones",
    detail:"Proponiendo mitigaciones en texto; no se modifica ningún archivo."
  },
  review: {
    label:"Revisando resultados",
    detail:"Contrastando las conclusiones con la evidencia disponible."
  }
};

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
  window.setTimeout(function () { toast.classList.remove("show"); }, 2800);
}

async function request(url, options) {
  var response;
  try {
    response = await fetch(url, options || {});
  } catch (error) {
    if (!options || !options.quiet) {
      showToast("No se pudo conectar con AudiAI. Comprueba que el servidor siga activo.");
    }
    return null;
  }
  if (!response.ok) {
    if (!options || !options.quiet) {
      var detail = "";
      try { detail = (await response.json()).detail || ""; } catch (error) { /* sin detalle JSON */ }
      showToast("Error " + response.status + (detail ? ": " + detail : ""));
    }
    return null;
  }
  return response.json();
}

function post(url, body, options) {
  var settings = {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body || {})
  };
  if (options && options.quiet) { settings.quiet = true; }
  return request(url, settings);
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

function wait(milliseconds) {
  return new Promise(function (resolve) { window.setTimeout(resolve, milliseconds); });
}

function paint() {
  return new Promise(function (resolve) {
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(resolve);
    });
  });
}

function formatDuration(milliseconds) {
  var safe = Math.max(0, Math.round(milliseconds));
  if (safe < 1000) { return Math.max(1, safe) + " ms"; }
  var seconds = safe / 1000;
  if (seconds < 10) { return seconds.toFixed(1).replace(".", ",") + " s"; }
  if (seconds < 60) { return Math.round(seconds) + " s"; }
  var minutes = Math.floor(seconds / 60);
  var rest = Math.round(seconds % 60);
  return minutes + " min" + (rest ? " " + rest + " s" : "");
}

function counted(count, singular, plural) {
  return count + " " + (count === 1 ? singular : plural);
}

function userMessageCount() {
  if (!state) { return 0; }
  return state.messages.filter(function (message) {
    return message.author === "user";
  }).length;
}

function lastActivity(session, kind) {
  var activities = (session && session.activities) || [];
  for (var index = activities.length - 1; index >= 0; index -= 1) {
    if (activities[index].kind === kind) { return activities[index]; }
  }
  return null;
}

function activeTurnNumber() {
  var count = userMessageCount();
  if (pendingTurn) { return count + 1; }
  return Math.max(1, count);
}

function ensureLiveWork(turnNumber) {
  if (liveWork && !liveWork.finishedAt) { return liveWork; }
  liveWork = {
    turn: turnNumber,
    startedAt: Date.now(),
    finishedAt: null,
    running: false,
    current: null,
    steps: []
  };
  return liveWork;
}

function beginStep(kind, label, detail) {
  if (!liveWork) { ensureLiveWork(activeTurnNumber()); }
  var step = {
    key: kind,
    title: label,
    detail: detail,
    status: "running",
    origin: ""
  };
  liveWork.steps.push(step);
  liveWork.current = step;
  liveWork.running = true;
  return step;
}

function completeStep(status, detail, origin) {
  if (!liveWork || !liveWork.current) { return; }
  liveWork.current.status = status || "completada";
  if (detail) { liveWork.current.detail = detail; }
  liveWork.current.origin = origin || "";
  liveWork.current = null;
  liveWork.running = false;
}

function finishLiveWork() {
  if (!liveWork || liveWork.finishedAt) { return; }
  if (liveWork.current) {
    liveWork.current.status = "fallida";
    liveWork.current.detail = "La conexión terminó antes de confirmar esta etapa.";
    liveWork.current = null;
  }
  liveWork.running = false;
  liveWork.finishedAt = Date.now();
  turnDurations[liveWork.turn] = liveWork.finishedAt - liveWork.startedAt;
  // El bloque se abre solo mientras hay trabajo en curso; al terminar vuelve
  // al resumen colapsado salvo que el usuario lo haya abierto después.
  delete openActivities[liveWork.turn];
}

function resetWorkState() {
  liveWork = null;
  pendingTurn = null;
  turnDurations = {};
  openActivities = {};
  autoApprovalScheduled = false;
  autoApprovalBlockedAction = null;
}

function activityToStep(activity) {
  return {
    key: activity.kind,
    title: activity.title,
    detail: activity.detail,
    status: activity.status,
    origin: activity.origin,
    duration_ms: activity.duration_ms
  };
}

function stepsDuration(steps) {
  return steps.reduce(function (total, step) {
    return total + (step.duration_ms || 0);
  }, 0);
}

function buildTurns() {
  var turns = [];
  var current = null;
  state.messages.forEach(function (message, index) {
    if (index === 0 && message.author === "agent") { return; }
    if (message.author === "user") {
      current = {number:turns.length + 1, user:message, agent:[]};
      turns.push(current);
      return;
    }
    if (!current) {
      current = {number:turns.length + 1, user:null, agent:[]};
      turns.push(current);
    }
    current.agent.push(message);
  });
  if (pendingTurn) {
    turns.push({number:turns.length + 1, user:{text:pendingTurn.text}, agent:[]});
  }
  (state.activities || []).forEach(function (activity) {
    var turn = turns[activity.turn - 1] || turns[turns.length - 1];
    if (!turn) { return; }
    turn.activities = turn.activities || [];
    turn.activities.push(activity);
  });
  if (liveWork) {
    var liveTurn = turns[liveWork.turn - 1] || turns[turns.length - 1];
    if (liveTurn) { liveTurn.live = liveWork; }
  }
  return turns;
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
  $("newSessionBtn").disabled = !!(liveWork && liveWork.running) || !!pendingTurn;
  $("welcome").hidden = !!pendingTurn || state.messages.some(function (message) {
    return message.author === "user";
  });
  $("downloadReportBtn").hidden = state.state !== "completa";

  if (liveWork && !liveWork.finishedAt && !state.pending_action &&
      (state.state === "completa" || state.state === "bloqueada")) {
    finishLiveWork();
  }

  renderSummaryMetrics();
  renderChat();
  renderReady();
  renderDocs();
  renderPatches();
  renderEvidence();

  var working = !!pendingTurn || !!(liveWork && liveWork.running);
  var input = $("messageInput");
  input.disabled = !!state.pending_action || working ||
    state.state === "completa" || state.state === "bloqueada";
  input.placeholder = working
    ? (liveWork && liveWork.current ? liveWork.current.title + "…" : "Pensando…")
    : state.pending_action
      ? "Esperando tu autorización…"
      : state.state === "completa"
        ? "La auditoría terminó; inicia una nueva revisión"
        : state.state === "bloqueada"
          ? "La revisión se detuvo; inicia una nueva sesión"
          : "Escribe una instrucción o pregunta";
  var send = $("sendBtn");
  send.disabled = input.disabled || working;
  send.classList.toggle("is-working", working);
  send.setAttribute("aria-busy", String(working));
  send.setAttribute("aria-label", working ? "Acciones en curso" : "Enviar mensaje");
  send.title = working ? "AudiAI está trabajando" : "Enviar mensaje";
  renderContextAttachment();
  scheduleAutomaticApproval();
}

function renderChat() {
  var host = $("chatLog");
  host.replaceChildren();
  var turns = buildTurns();
  var lastBlock = null;
  turns.forEach(function (turn) {
    var block = renderTurn(turn);
    if (block) {
      host.appendChild(block);
      lastBlock = block;
    }
  });
  if (state.pending_action && !(liveWork && liveWork.running)) {
    var card = renderPendingCard(state.pending_action);
    if (lastBlock) { lastBlock.appendChild(card); }
    else { host.appendChild(card); }
  }
  var area = $("chatArea");
  area.scrollTop = area.scrollHeight;
}

function renderTurn(turn) {
  var hasWork = !!turn.live || !!(turn.activities && turn.activities.length);
  if (!turn.user && !turn.agent.length && !hasWork) { return null; }

  var block = element("section", "turn");
  block.dataset.turn = String(turn.number);
  if (turn.user) {
    var bubble = element("article", "msg user");
    bubble.setAttribute("aria-label", "Tu mensaje");
    var copy = element("div", "message-copy");
    renderFormattedMessage(copy, turn.user.text);
    bubble.appendChild(copy);
    block.appendChild(bubble);
  }

  if (hasWork) { block.appendChild(renderWorkBlock(turn)); }

  if (pendingTurn && !pendingTurn.isAudit && turn.number === activeTurnNumber()) {
    var typing = element("article", "msg agent");
    var draft = element("div", "message-copy");
    draft.id = "streamingDraft";
    if (pendingTurn.draft) {
      // Ya hay texto del modelo: se muestra como respuesta, sin "Pensando…".
      renderFormattedMessage(draft, pendingTurn.draft);
      typing.appendChild(draft);
    } else {
      var indicator = element("div", "typing-indicator");
      indicator.appendChild(element("span"));
      indicator.appendChild(element("span"));
      indicator.appendChild(element("span"));
      typing.appendChild(indicator);
    }
    block.appendChild(typing);
    return block;
  }

  if (turn.agent.length) {
    var visible = hasWork ? turn.agent.slice(-1) : turn.agent;
    visible.forEach(function (message) {
      var reply = element("article", "msg agent");
      reply.setAttribute("aria-label", "Respuesta de AudiAI");
      var body = element("div", "message-copy");
      renderFormattedMessage(body, message.text);
      reply.appendChild(body);
      block.appendChild(reply);
    });
  }
  return block;
}

function renderWorkBlock(turn) {
  var live = turn.live;
  var steps = live
    ? live.steps
    : (turn.activities || []).map(activityToStep);
  if (!steps.length) { return null; }

  var finished = live ? !!live.finishedAt : true;
  var running = !!(live && live.running);
  var waiting = !!(live && !finished && !running && state.pending_action);
  var details = element("details", "activity-disclosure");
  details.dataset.turn = String(turn.number);
  if (running) {
    details.open = true;
  } else if (waiting) {
    // Al pedir permiso, el resumen debe quedar visible y el detalle a un clic.
    details.open = false;
  } else {
    details.open = openActivities[turn.number] !== undefined
      ? openActivities[turn.number]
      : false;
  }
  details.setAttribute("aria-busy", String(running || waiting));

  var summary = element("summary", "activity-summary");
  if (running) {
    summary.appendChild(element("span", "activity-spinner", ""));
  } else if (waiting) {
    summary.appendChild(element("span", "activity-wait", ""));
  } else {
    var check = element("span", "activity-check", "✓");
    check.setAttribute("aria-hidden", "true");
    summary.appendChild(check);
  }

  var title;
  if (running) {
    title = (live.current ? live.current.title : "Trabajando") + "…";
  } else if (waiting) {
    title = "Esperando tu autorización";
  } else {
    var elapsed = live && live.finishedAt
      ? turnDurations[turn.number]
      : stepsDuration(steps);
    title = "Trabajó durante " + formatDuration(elapsed || 0);
  }
  summary.appendChild(element("span", "activity-title", title));

  if (running) {
    var clock = element("span", "activity-elapsed");
    clock.dataset.workElapsed = "1";
    clock.textContent = "· " + formatDuration(Date.now() - live.startedAt);
    summary.appendChild(clock);
  } else {
    summary.appendChild(element(
      "span",
      "activity-elapsed",
      "· " + counted(steps.length, "paso", "pasos")
    ));
  }
  details.appendChild(summary);

  var body = element("div", "activity-body");
  var list = element("div", "activity-steps");
  steps.forEach(function (step) {
    list.appendChild(renderStep(step));
  });
  body.appendChild(list);

  if (turn.agent.length > 1 || (hasWorkNotes(turn))) {
    var notes = element("details", "activity-notes");
    notes.appendChild(element("summary", "", "Lo que hizo el agente"));
    var noteList = element("div", "activity-note-list");
    var narration = turn.agent.length > 1 ? turn.agent.slice(0, -1) : turn.agent;
    narration.forEach(function (message) {
      var note = element("div", "activity-note");
      renderFormattedMessage(note, message.text);
      noteList.appendChild(note);
    });
    if (narration.length) {
      notes.appendChild(noteList);
      body.appendChild(notes);
    }
  }

  body.appendChild(element(
    "span",
    "activity-footnote",
    "Registro de acciones reales; no incluye razonamiento interno privado del modelo."
  ));
  details.appendChild(body);
  return details;
}

function hasWorkNotes(turn) {
  return !!turn.live && turn.agent.length > 1;
}

function renderStep(step) {
  var row = element("div", "activity-step");
  row.dataset.status = step.status === "running" ? "running" : step.status;
  var marker = element("span", "step-marker");
  if (step.status === "fallida") { marker.textContent = "!"; }
  else if (step.status === "omitida") { marker.textContent = "–"; }
  else if (step.status !== "running") { marker.textContent = "✓"; }
  marker.setAttribute("aria-hidden", "true");
  row.appendChild(marker);

  var copy = element("div", "step-copy");
  copy.appendChild(element("strong", "", step.title));
  copy.appendChild(element("span", "", step.detail));
  if (step.origin) {
    copy.appendChild(element("span", "step-origin", step.origin));
  }
  row.appendChild(copy);
  return row;
}

function renderPendingCard(pending) {
  var card = element("div", "action-card");
  var pendingIndex = stageIndex(pending.kind);
  var sensitive = pending.kind === "sensitive";
  var externalConsent = (pending.kind === "security" ||
    (pending.kind === "analysis" && state.agent_mode === "agent")) &&
    usesExternalModel() && !state.external_data_consent;
  card.appendChild(element("div", "action-eyebrow",
    sensitive ? "PERMISO PARA ARCHIVOS CONFIDENCIALES" :
    externalConsent ? "PERMISO PARA USAR EL MODELO CONECTADO" :
    "ETAPA " + (pendingIndex + 1) + " DE " + activeWorkflowStages().length));
  card.appendChild(element("div", "caption", pending.caption));
  card.appendChild(element("div", "hint", pending.hint));
  if (sensitive || externalConsent) {
    var modelLabel = currentModelLabel();
    var disclosure = sensitive
      ? "Solo se abrirán los archivos enumerados arriba. No se modificarán."
      : "La evidencia y el contexto autorizado se enviarán al modelo conectado" +
        (modelLabel ? " (" + modelLabel + ")" : "") + ". " +
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
  return card;
}

function renderContextAttachment() {
  var attachment = $("contextAttachment");
  var removeButton = $("removeContextBtn");
  attachment.hidden = !selectedProjectFolder;
  if (!selectedProjectFolder) { return; }
  var folderName = selectedProjectFolder.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  $("contextAttachmentLabel").textContent = "Proyecto · " + (folderName || selectedProjectFolder);
  attachment.title = selectedProjectFolder;
  removeButton.disabled = state.state !== "esperando_ruta" ||
    !!pendingTurn || !!(liveWork && liveWork.running);
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
  var openRouter = state.roles_provider === "openrouter";
  var localModel = state.roles_provider === "lmstudio";
  var reportedModel = state.chat_model || state.analysis_model;
  var displayModel = reportedModel || state.configured_model || "";
  var selector = $("modelSelector");
  var nextSelection = modelChoiceInitialized
    ? selector.value || selectedModel
    : "local";
  selector.replaceChildren();
  if (openRouter) {
    selector.appendChild(new Option(
      displayModel ? formatModelName(displayModel) : "Modelo conectado",
      "external"
    ));
  } else {
    selector.appendChild(new Option(
      localModel ? "LM Studio · local" : "Auto",
      "local"
    ));
  }
  if (configured) {
    selector.appendChild(new Option("OpenRouter", "external"));
  }
  selectedModel = openRouter
    ? "external"
    : configured && nextSelection === "external" ? "external" : "local";
  modelChoiceInitialized = true;
  selector.value = selectedModel;
  selector.disabled = !!state.external_data_consent || openRouter;
  var label = displayModel
    ? formatModelName(displayModel)
    : selectedModel === "external" ? "Modelo conectado" : "Auto";
  $("modelChip").textContent = label;

  var badge = $("providerBadge");
  if (openRouter) {
    badge.textContent = displayModel
      ? formatModelName(displayModel)
      : "Modelo conectado";
    badge.className = "mode-badge is-real";
  } else if (configured) {
    badge.textContent = "Subagente IA conectado";
    badge.className = "mode-badge is-real";
  } else if (localModel) {
    badge.textContent = "Modelo local · LM Studio";
    badge.className = "mode-badge is-local";
  } else {
    badge.textContent = "Modo simulado · sin red";
    badge.className = "mode-badge is-mock";
  }
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
  $("analysisModeHint").textContent = analysisHints[$("analysisModeSelector").value];
  $("agentModeHint").textContent = agentHints[$("agentModeSelector").value];
  $("modelDescription").textContent = selectedModel === "external"
    ? (currentModelLabel()
      ? "El modelo " + currentModelLabel() +
        " recibe evidencia solo después de tu autorización."
      : "El modelo conectado recibe evidencia solo después de tu autorización.")
    : state.roles_provider === "lmstudio"
      ? "Modelo local: las solicitudes se envían a LM Studio en este equipo."
      : state.roles_provider === "strands" || state.roles_provider === "openrouter"
      ? "Auto evita el envío externo; la auditoría continúa localmente."
      : "Auto usa el modo simulado; no hay proveedor conectado.";
}

function currentModelLabel() {
  var model = state.chat_model || state.analysis_model ||
    state.configured_model || "";
  return model ? formatModelName(model) : "";
}

function usesExternalModel() {
  return state.roles_provider === "openrouter" ||
    (state.roles_provider === "strands" && selectedModel === "external");
}

function activeWorkflowStages() {
  return state && state.agent_mode === "agent"
    ? workflowStages.filter(function (stage) {
      return ["list", "checks", "analysis"].indexOf(stage.id) !== -1;
    })
    : workflowStages;
}

function stageIndex(stageId) {
  return activeWorkflowStages().findIndex(function (stage) {
    return stage.id === stageId;
  });
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
    usesExternalModel() &&
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
    !action || pendingTurn || (liveWork && liveWork.running) ||
    needsUserApproval(action) ||
    autoApprovalBlockedAction === action.action_id || autoApprovalScheduled
  ) { return; }
  autoApprovalScheduled = true;
  window.setTimeout(function () {
    autoApprovalScheduled = false;
    if (state && state.pending_action &&
        state.pending_action.action_id === action.action_id &&
        !(liveWork && liveWork.running)) {
      if (shouldSkipAutomatically(action)) { onSkip(null, true); }
      else { onApprove(null, true); }
    }
  }, 0);
}

async function onApprove(event, automatic) {
  if (!state || !state.pending_action || pendingTurn ||
      (liveWork && liveWork.running)) { return; }
  var pending = state.pending_action;
  autoApprovalBlockedAction = null;
  ensureLiveWork(activeTurnNumber());
  var copy = STEP_COPY[pending.kind] || {
    label:"Procesando",
    detail:"Ejecutando esta etapa de la revisión."
  };
  beginStep(pending.kind, copy.label, copy.detail);
  render();
  var started = Date.now();
  var beforeActivities = (state.activities || []).length;
  await paint();

  var updated = await post(
    "/agent/sessions/" + encodeURIComponent(state.session_id) +
    "/approve/" + encodeURIComponent(pending.action_id),
    {},
    {quiet:true}
  );
  if (!updated) {
    updated = await fetchSession();
  }
  var activity = updated ? lastActivity(updated, pending.kind) : null;
  var elapsed = Date.now() - started;
  if (elapsed < MIN_STEP_MS) { await wait(MIN_STEP_MS - elapsed); }
  completeStep(
    activity ? activity.status : "fallida",
    activity ? activity.detail : "No se pudo confirmar el resultado con el servidor.",
    activity ? activity.origin : "fallo declarado"
  );
  if (updated) {
    state = updated;
    render();
    if ((updated.activities || []).length <= beforeActivities &&
        updated.pending_action &&
        updated.pending_action.action_id === pending.action_id) {
      showToast("La etapa no se completó; puedes reintentarla.");
    }
    return;
  }
  if (automatic) { autoApprovalBlockedAction = pending.action_id; }
  render();
  showToast("La acción pudo completarse; sincronizando el estado.");
}

async function onSkip(event, automatic) {
  if (!state || !state.pending_action || pendingTurn ||
      (liveWork && liveWork.running)) { return; }
  var pending = state.pending_action;
  ensureLiveWork(activeTurnNumber());
  beginStep(
    pending.kind,
    pending.kind === "sensitive" ? "Omitiendo archivos" : "Continuando sin IA externa",
    "No se leerán archivos omitidos ni se enviará evidencia al proveedor."
  );
  render();
  var started = Date.now();
  var beforeActivities = (state.activities || []).length;
  await paint();
  var updated = await post(
    "/agent/sessions/" + encodeURIComponent(state.session_id) +
    "/skip/" + encodeURIComponent(pending.action_id),
    {},
    {quiet:true}
  );
  if (!updated) { updated = await fetchSession(); }
  var activity = updated ? lastActivity(updated, pending.kind) : null;
  var elapsed = Date.now() - started;
  if (elapsed < MIN_STEP_MS) { await wait(MIN_STEP_MS - elapsed); }
  completeStep(
    activity ? activity.status : "omitida",
    activity ? activity.detail : "La etapa se omitió por decisión del usuario.",
    activity ? activity.origin : "decisión del usuario"
  );
  if (updated) {
    state = updated;
    render();
    if ((updated.activities || []).length <= beforeActivities &&
        updated.pending_action &&
        updated.pending_action.action_id === pending.action_id) {
      showToast("No se pudo registrar la decisión; revisa el estado.");
    }
    return;
  }
  if (automatic) { autoApprovalBlockedAction = pending.action_id; }
  render();
  showToast("No se pudo completar la decisión; sincronizando.");
}

async function onSend(event) {
  event.preventDefault();
  if (!state) { return; }
  if (pendingTurn || (liveWork && liveWork.running)) { return; }
  if (state.pending_action) { return; }
  var input = $("messageInput");
  var text = input.value.trim();
  if (!text) { return; }
  var attachedProject = state.state === "esperando_ruta" ? selectedProjectFolder : null;
  var startsAudit = !!attachedProject ||
    /^[A-Za-z]:[\\/]|^\\\\|^\/|^\.{1,2}[\\/]|^~[\\/]/.test(text);

  pendingTurn = {text:text, isAudit:startsAudit, draft:""};
  if (startsAudit) {
    ensureLiveWork(activeTurnNumber());
    beginStep(
      "think",
      STEP_COPY.think.label,
      startsAudit
        ? "Comprobando la carpeta y preparando la lectura en solo lectura."
        : STEP_COPY.think.detail
    );
  }
  render();
  await paint();

  var messageUrl = "/agent/sessions/" +
    encodeURIComponent(state.session_id) + "/messages";
  var messageBody = {
    text:text,
    analysis_mode:$("analysisModeSelector").value,
    agent_mode:$("agentModeSelector").value
  };
  if (attachedProject) { messageBody.project_path = attachedProject; }

  var updated = !startsAudit && state.state === "esperando_ruta"
    ? await postChatStream(messageUrl + "/stream", messageBody, function (delta) {
      if (pendingTurn) {
        pendingTurn.draft += delta;
        var draft = $("streamingDraft");
        if (draft) {
          renderFormattedMessage(draft, pendingTurn.draft);
          var area = $("chatArea");
          area.scrollTop = area.scrollHeight;
        }
      }
    })
    : await post(messageUrl, messageBody);

  if (startsAudit && liveWork && liveWork.current) {
    completeStep("completada", "Solicitud interpretada y alcance registrado.");
  }
  if (updated) {
    state = updated;
    input.value = "";
    pendingTurn = null;
    render();
    return;
  }
  if (liveWork && liveWork.current) {
    completeStep("fallida", "No se pudo confirmar la solicitud con el servidor.");
  }
  pendingTurn = null;
  render();
  await reconcileSession("Sincronizando la conversación con el servidor.");
}

async function fetchSession() {
  if (!state) { return null; }
  return request(
    "/agent/sessions/" + encodeURIComponent(state.session_id),
    {method:"GET"}
  );
}

async function reconcileSession(message) {
  var current = await fetchSession();
  if (current) {
    state = current;
    render();
    showToast(message);
  }
}

async function newSession(announce) {
  var created = await post("/agent/sessions", {}, {quiet:true});
  if (created) {
    state = created;
    selectedProjectFolder = null;
    resetWorkState();
    togglePanel(false);
    render();
    return;
  }
  // Recupera el caso en que el servidor creó la sesión pero se perdió la
  // respuesta HTTP: el servidor es de un solo usuario y vive en memoria.
  var summaries = await request("/agent/sessions", {method:"GET", quiet:true});
  if (!summaries || !summaries.length) {
    showToast("No se pudo iniciar una sesión de auditoría.");
    return;
  }
  summaries.sort(function (a, b) {
    return Number(b.session_id.replace("chat-", "")) -
      Number(a.session_id.replace("chat-", ""));
  });
  var recovered = await request(
    "/agent/sessions/" + encodeURIComponent(summaries[0].session_id),
    {method:"GET", quiet:true}
  );
  if (recovered) {
    state = recovered;
    selectedProjectFolder = null;
    resetWorkState();
    togglePanel(false);
    render();
    if (announce) {
      showToast("Sesión recuperada tras una respuesta de red interrumpida.");
    }
    return;
  }
  showToast("No se pudo recuperar la sesión de auditoría.");
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
$("newSessionBtn").addEventListener("click", function () { newSession(true); });
$("panelToggle").addEventListener("click", function () { togglePanel(); });
$("closePanel").addEventListener("click", function () { togglePanel(false); });
document.querySelectorAll("[data-panel-tab]").forEach(function (tab) {
  tab.addEventListener("click", function () { setPanelTab(tab.dataset.panelTab); });
});
document.addEventListener("toggle", function (event) {
  var target = event.target;
  if (!target || !target.classList ||
      !target.classList.contains("activity-disclosure")) { return; }
  openActivities[target.dataset.turn] = target.open;
}, true);
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

window.setInterval(function () {
  if (!liveWork || !liveWork.running) { return; }
  var clock = document.querySelector("[data-work-elapsed]");
  if (clock) {
    clock.textContent = "· " + formatDuration(Date.now() - liveWork.startedAt);
  }
}, 250);

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
  if (state && state.state === "esperando_ruta" && !pendingTurn &&
      !(liveWork && liveWork.running)) {
    selectedProjectFolder = null;
    renderContextAttachment();
  }
});

setWelcomeName("");
newSession();
