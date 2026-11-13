(function () {
  "use strict";
  const API = "/api";
  const content = document.getElementById("content");
  const crumb = document.getElementById("crumb-title");
  const connection = document.getElementById("connection-state");
  const modal = document.getElementById("modal");
  const toasts = document.getElementById("toasts");
  const themeButton = document.getElementById("theme-toggle");
  const viewNames = { home: "Visão geral", setup: "Primeiros passos", courses: "Cursos e atividades", library: "Arquivos e materiais", assistant: "Assistente de estudos", automation: "Automações", runs: "Execuções", settings: "Conexões e modelos" };
  const runChip = document.getElementById("run-chip");
  const runsBadge = document.getElementById("runs-badge");
  const JOB_STATUS = { queued: "Na fila", running: "Em execução", success: "Concluída", partial: "Concluída com avisos", error: "Falhou", cancelled: "Cancelada", interrupted: "Interrompida" };
  // O anel em CSS gira centrado; o glifo "↻" girava fora do eixo.
  const BUSY_RING = '<i class="busy-ring" aria-hidden="true"></i>';
  const JOB_ICON = { queued: "⋯", running: BUSY_RING, success: "✓", partial: "!", error: "✕", cancelled: "■", interrupted: "■" };
  const ASSIGNMENT_STATUS = { pendente: "Pendente", atrasada: "Atrasada", concluida: "Concluída", indisponivel: "Indisponível" };
  const STEP_STATUS = { pending: "Aguardando", running: "Em andamento", done: "Concluída", error: "Falhou", cancelled: "Cancelada", skipped: "Não executada" };
  const LOG_FILTERS = [["all", "Todo o log"], ["error", "Erros"], ["warn", "Avisos"], ["ok", "Baixados / concluídos"], ["copy", "Copiados"], ["skip", "Ignorados"], ["info", "Informações"]];
  const MAX_LOG_LINES = 3000;
  const SETUP_DISMISSED_KEY = "puc-canvas-setup-dismissed";
  const providers = {
    openai: { title: "OpenAI / GPT", base: "https://api.openai.com/v1", model: "gpt-6-luna", keyLabel: "Chave da API OpenAI" },
    anthropic: { title: "Anthropic / Claude", base: "https://api.anthropic.com/v1", model: "claude-sonnet-5-5", keyLabel: "Chave da API Anthropic" },
    google: { title: "Google / Gemini", base: "https://generativelanguage.googleapis.com/v1beta", model: "gemini-3.8-flash", keyLabel: "Chave da API Gemini" },
    local: { title: "Modelo local", base: "http://127.0.0.1:1234/v1", model: "", keyLabel: "Chave, se exigida pelo servidor" },
    compatible: { title: "Outro endpoint compatível", base: "", model: "", keyLabel: "Token do provedor" }
  };
  let view = "home";
  let data = null;
  let config = null;
  let jobs = { current: null, queue: [], history: [] };
  let selectedRunId = null;
  let runDetail = null;
  let runLog = { id: null, lines: [], next: 0 };
  let logFilter = "all";
  let libraryTimer = null;
  let chatHistory = [];
  let courseQuery = "";
  let courseFilter = "all";
  let statusFilter = "all";
  const STATUS_FILTERS = [["all", "Todos os status"], ["open", "Pendentes e atrasadas"], ["atrasada", "Só atrasadas"], ["pendente", "Só pendentes no prazo"], ["concluida", "Concluídas"]];
  let libraryQuery = "";
  let libraryType = "all";
  let libraryResults = [];
  let pending = false;
  let initialRouteResolved = false;

  function applyTheme(theme, persist) {
    const selected = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = selected;
    if (themeButton) {
      const dark = selected === "dark";
      themeButton.innerHTML = '<span aria-hidden="true">' + (dark ? "☀" : "☾") + '</span>';
      themeButton.title = dark ? "Ativar modo claro" : "Ativar modo noturno";
      themeButton.setAttribute("aria-label", themeButton.title);
      themeButton.setAttribute("aria-pressed", String(dark));
    }
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = selected === "dark" ? "#101714" : "#f3f6f3";
    if (persist) {
      try { localStorage.setItem("puc-theme", selected); } catch (_) {}
    }
  }
  function setupDismissed() {
    try { return localStorage.getItem(SETUP_DISMISSED_KEY) === "1"; } catch (_) { return false; }
  }
  function dismissCanvasSetup() {
    try { localStorage.setItem(SETUP_DISMISSED_KEY, "1"); } catch (_) {}
    setView("home");
  }

  async function request(path, options) {
    const response = await fetch(API + path, Object.assign({ cache: "no-store" }, options || {}));
    let result = {};
    try { result = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(result.message || result.error || "Não foi possível concluir a operação.");
    return result;
  }
  async function post(path, value) {
    return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value || {}) });
  }
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
  }
  function when(value, includeTime) {
    if (!value) return "Sem prazo";
    const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? value + "T12:00:00" : value);
    if (Number.isNaN(date.getTime())) return "Sem prazo";
    return new Intl.DateTimeFormat("pt-BR", includeTime ? { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" } : { day: "2-digit", month: "short" }).format(date);
  }
  function daysUntil(value) {
    if (!value) return null;
    const due = new Date(value);
    return Math.ceil((due.getTime() - Date.now()) / 86400000);
  }
  function toast(message, actionLabel, onAction) {
    const item = document.createElement("div");
    item.className = "toast";
    item.textContent = message;
    if (actionLabel && onAction) {
      const button = document.createElement("button");
      button.className = "link-button toast-action";
      button.textContent = actionLabel;
      button.addEventListener("click", function () { item.remove(); onAction(); });
      item.appendChild(button);
    }
    toasts.appendChild(item);
    setTimeout(function () { item.remove(); }, actionLabel ? 8000 : 3600);
  }
  function ago(value) {
    if (!value) return "nunca";
    const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
    if (seconds < 60) return "agora há pouco";
    if (seconds < 3600) return "há " + Math.round(seconds / 60) + " min";
    if (seconds < 86400) return "há " + Math.round(seconds / 3600) + " h";
    return "há " + Math.round(seconds / 86400) + " dias";
  }
  function duration(start, end) {
    if (!start) return "—";
    const seconds = Math.max(0, Math.round(((end ? new Date(end) : new Date()).getTime() - new Date(start).getTime()) / 1000));
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    return (h ? h + " h " : "") + (h || m ? m + " min " : "") + s + " s";
  }
  function isRunning() { return Boolean(jobs.current && jobs.current.status === "running"); }
  function queueList() { return jobs.queue || []; }
  // Ação sem filtro de matéria/atividade que já está rodando ou na fila.
  function activeEntryFor(action) {
    const plain = function (job) { return job && job.action === action && !(job.params && (job.params.course || job.params.assignment_id)); };
    if (isRunning() && plain(jobs.current)) return jobs.current;
    return queueList().find(plain) || null;
  }
  function jobStatusText(job) { return job ? (JOB_STATUS[job.status] || job.status_label || job.status) : ""; }
  function progressPercent(job) {
    const p = job && job.progress;
    if (!p || !p.total) return null;
    return Math.max(0, Math.min(100, Math.round(p.current / p.total * 100)));
  }
  function progressText(job) {
    const p = job && job.progress;
    if (!p || !p.total) return job && job.status === "running" ? "Iniciando…" : "";
    const unit = { cursos: "cursos", atividades: "atividades", pdfs: "PDFs" }[p.step] || p.step;
    if (job.status !== "running" || p.current >= p.total) return p.current + " de " + p.total + " " + unit + " concluídos";
    return "processando " + (p.current + 1) + " de " + p.total + " " + unit + (p.label ? " · " + p.label : "");
  }
  function currentStep(job) {
    if (!job || !job.steps || !job.steps.length) return null;
    return job.steps[job.step_index] || job.steps[0];
  }
  function setView(next, anchorId) {
    view = next;
    if (view === "runs" && !selectedRunId && allRuns().length) {
      selectedRunId = allRuns()[0].id;
      loadRunDetail(selectedRunId).then(function () { if (view === "runs") render(); }).catch(function () {});
    }
    crumb.textContent = viewNames[view] || viewNames.home;
    document.querySelectorAll(".nav[data-view]").forEach(function (button) { button.classList.toggle("active", button.dataset.view === view); });
    render();
    const anchor = anchorId ? document.getElementById(anchorId) : null;
    // Rolagem instantânea: a suave pode ser ignorada pelo navegador e deixar o
    // usuário no topo da página, longe da lista que ele pediu.
    if (anchor) anchor.scrollIntoView({ block: "start" });
    else window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function setConnection() {
    const canvasOk = Boolean(config && config.canvas && config.canvas.configured);
    const aiOk = Boolean(config && config.llm && config.llm.configured);
    connection.className = "connection " + (canvasOk ? "ok" : "bad");
    const lastSync = data && data.canvas ? data.canvas.last_sync : null;
    connection.innerHTML = "<i></i>" + (canvasOk ? "Canvas configurado" : "Configure o Canvas") + (aiOk ? " · IA pronta" : "") + (canvasOk ? " · sincronizado " + esc(ago(lastSync)) : "");
    connection.title = lastSync ? "Última sincronização: " + when(lastSync, true) : "Ainda não sincronizado";
  }
  function courseList() { return data && Array.isArray(data.courses) ? data.courses : []; }
  function assignmentList() { return data && Array.isArray(data.assignments) ? data.assignments : []; }
  function activeModelText() {
    if (!config || !config.llm || !config.llm.configured) return "Nenhum modelo configurado";
    return (config.llm.provider_label || config.llm.provider || "IA") + " · " + (config.llm.model || "modelo");
  }
  function stat(icon, tone, title, value, detail) {
    return '<article class="stat"><span class="stat-icon tone-' + tone + '">' + icon + '</span><div><small>' + title + '</small><strong>' + value + '</strong><p>' + detail + '</p></div></article>';
  }
  function courseCard(course) {
    const items = assignmentList().filter(function (item) { return item.course_key === course.key; });
    const pendingItems = items.filter(function (item) { return item.status === "pendente" || item.status === "atrasada"; }).length;
    const mark = (course.name || course.key).split(/\s+/).map(function (x) { return x[0] || ""; }).join("").slice(0, 2).toUpperCase();
    const status = course.validity_status || "active";
    const badge = status === "expired" ? "Acompanhamento expirado" : status === "soon" ? "Vence em " + course.days_remaining + " dias" : "Válido por " + course.days_remaining + " dias";
    return '<article class="card"><div class="course-top"><span class="course-code">' + esc(mark) + '</span><span class="badge ' + (status === "expired" ? "overdue" : status === "soon" ? "soon" : "") + '">' + esc(badge) + '</span></div><h3>' + esc(course.name) + '</h3><div class="meta">' + Number(course.total || 0) + ' atividades · ' + Number(course.materials || 0) + ' arquivos</div><div class="course-bottom"><span>Validade local até <strong>' + when(course.expires_at) + '</strong></span><button class="link-button" data-course-filter="' + esc(course.key) + '">' + pendingItems + ' pendentes →</button></div></article>';
  }
  function setupNotice() {
    if (!config) return "";
    const missing = [];
    if (!config.canvas.configured) missing.push("conectar o Canvas");
    if (!config.llm.configured) missing.push("adicionar um modelo de IA");
    if (!missing.length) return "";
    return '<div class="notice warn setup-notice"><span>!</span><div>Para começar, ' + missing.join(" e ") + '. <button class="link-button" data-go="settings">Abrir configuração →</button></div></div>';
  }
  function setupView() {
    const canvas = config && config.canvas || {};
    return '<header class="page-head"><div><p class="eyebrow">PRIMEIROS PASSOS · 1 DE 2</p><h1>Vamos conectar seu Canvas.</h1><p>Essa configuração fica salva só neste computador. Depois, sincronize seus cursos para ver prazos e materiais.</p></div></header>' +
      '<div class="onboarding-layout"><section class="panel onboarding-card"><div class="onboarding-step"><span>01</span><div><h2>Conexão com o Canvas</h2><p>Informe o endereço usado pela sua instituição e o token pessoal da sua conta.</p></div></div>' +
      '<div class="setup-instructions"><b>Como criar seu token</b><ol><li>Entre no Canvas da sua instituição.</li><li>Abra <strong>Configurações</strong> e localize <strong>Tokens de acesso</strong>.</li><li>Crie um token pessoal e cole-o aqui. Guarde-o como uma senha.</li></ol></div>' +
      '<form id="first-canvas-form" class="form"><div class="field"><label for="first-canvas-url">Endereço do Canvas</label><input id="first-canvas-url" name="canvas_base_url" type="url" required placeholder="https://canvas.sua-instituicao.br" value="' + esc(canvas.base_url || "") + '"><small>Use o endereço principal, sem uma página de atividade específica.</small></div><div class="field"><label for="first-canvas-token">Token pessoal do Canvas</label><input id="first-canvas-token" name="canvas_token" type="password" required autocomplete="new-password" placeholder="Cole o token criado nas configurações"></div><div class="inline-actions"><button class="button primary" type="submit" ' + (pending ? "disabled" : "") + '>' + (pending ? "Conectando…" : "Salvar e testar conexão") + '</button><button class="button quiet" type="button" data-dismiss-setup>Configurar depois</button></div></form>' +
      '<div class="notice onboarding-notice"><span>i</span><div>O token fica salvo neste computador e é usado para acessar os dados da sua conta Canvas.</div></div></section>' +
      '<aside class="panel onboarding-side"><div class="onboarding-step"><span>02</span><div><h2>Modelo de IA (opcional)</h2><p>Depois você pode escolher GPT, Claude, Gemini ou um modelo local e adicionar sua chave.</p></div></div><button class="button quiet" data-go="settings">Ver opções de IA →</button><div class="onboarding-help"><b>Precisa de ajuda?</b><small>Você pode fazer essa etapa depois. O modo de configuração também fica disponível em Conexões e modelos.</small></div></aside></div>';
  }
  function homeView() {
    const stats = data ? data.stats || {} : {};
    const courses = courseList().slice(0, 3);
    // "Próximo prazo" mostra o que ainda vence; as atrasadas têm aviso próprio.
    const focus = data ? ((data.today || []).concat(data.upcoming || []))[0] : null;
    const overdueCount = data && data.overdue ? data.overdue.length : 0;
    return '<div class="page-head"><div><p class="eyebrow">' + new Intl.DateTimeFormat("pt-BR", { weekday: "long", day: "2-digit", month: "long" }).format(new Date()).toUpperCase() + '</p><h1>Seu Canvas, mais simples.</h1><p>Veja prazos, encontre materiais e peça ajuda ao modelo que você escolher.</p></div><div class="head-actions"><button class="button quiet" data-go="assistant">✳ Perguntar à IA</button>' + runButton("update", "↻ Sincronizar Canvas", "button primary") + '</div></div>' +
      setupNotice() + '<div data-job-status></div>' +
      '<section class="hero"><div class="hero-copy"><p class="eyebrow">CANVAS COMPANION</p><h2>Organize as matérias. Encontre o que precisa. Estude com ajuda.</h2><p>Seus dados do Canvas e arquivos ficam no computador. O assistente pesquisa apenas o conteúdo sincronizado e os links continuam levando ao Canvas.</p></div><span class="hero-stamp">∿</span></section>' +
      '<section class="stats" aria-label="Resumo">' +
      stat("◷", "amber", "ENTREGAS HOJE", stats.today || 0, (stats.pending || 0) + " atividades pendentes") +
      stat("▤", "blue", "MATÉRIAS", courseList().length, "cursos sincronizados") +
      stat("◇", "green", "MATERIAIS", stats.materials || 0, (stats.summaries || 0) + " resumos prontos") +
      stat("✳", "red", "MODELO ATIVO", config && config.llm && config.llm.configured ? "Pronto" : "—", activeModelText()) +
      '</section>' +
      '<div class="section-title"><div><h2>Suas matérias</h2><p>Resumo dos cursos que vieram do Canvas.</p></div><button class="link-button" data-go="courses">Ver cursos e atividades →</button></div>' +
      (courses.length ? '<section class="grid">' + courses.map(courseCard).join("") + '</section>' : '<section class="empty"><b>Seus cursos aparecerão aqui</b>Conecte o Canvas e sincronize para trazer suas matérias e prazos.<br><button class="button primary small" data-go="settings">Configurar conexão</button></section>') +
      '<div class="stats-row"><section class="panel"><div class="panel-head"><div><h2>Próximo prazo</h2><p>Atividades que merecem sua atenção.</p></div><button class="link-button" data-go="courses">Agenda completa</button></div>' +
      (focus ? '<div class="deadline"><span class="datebox"><strong>' + when(focus.due_at).split(" ")[0] + '</strong><small>prazo</small></span><div><strong>' + esc(focus.title) + '</strong><small>' + esc(focus.course) + ' · ' + esc(ASSIGNMENT_STATUS[focus.status] || focus.status) + '</small></div><time>' + when(focus.due_at, true) + '</time></div>' : '<div class="empty">Nenhum prazo futuro. Sincronize o Canvas para atualizar a agenda.</div>') +
      (overdueCount ? '<div class="notice warn overdue-note"><span>!</span><div>' + overdueCount + (overdueCount === 1 ? ' atividade atrasada' : ' atividades atrasadas') + ' sem entrega. <button class="link-button" data-go="courses">Ver lista →</button></div></div>' : "") +
      '</section><section class="panel model-box"><div class="panel-head"><div><h2>Seu assistente</h2><p>O modelo selecionado pode buscar arquivos e itens.</p></div></div>' +
      '<div class="model-current"><span>✳</span><div><b>' + esc(activeModelText()) + '</b><small>' + (config && config.llm && config.llm.configured ? "Conexão salva neste computador" : "Adicione sua chave ou escolha um modelo local") + '</small></div></div><button class="button quiet small" data-go="assistant">Abrir assistente →</button></section></div>';
  }
  // Com outra automação rodando, o botão coloca esta na fila em vez de bloquear.
  function runButton(action, label, classes) {
    const active = activeEntryFor(action);
    const text = active ? (active.status === "running" ? "Em execução…" : "Na fila (" + active.position + "º)") : isRunning() ? "+ Adicionar à fila" : label;
    const title = isRunning() && !active ? ' title="Será executada quando a automação atual terminar"' : "";
    return '<button class="' + classes + '" data-run="' + action + '"' + (pending || active ? " disabled" : "") + title + '>' + text + '</button>';
  }
  // Cartão compacto da execução atual (ou da última), atualizado pelo polling
  // sem redesenhar a página inteira.
  function jobStatusBlock(showFinished) {
    const job = jobs.current;
    if (!job || (!showFinished && job.status !== "running")) return "";
    const percent = progressPercent(job);
    const step = currentStep(job);
    const counts = job.counts || {};
    const tone = job.status === "error" ? " warn" : job.status === "partial" ? " warn" : "";
    return '<div class="notice job-notice' + tone + '"><span>' + (JOB_ICON[job.status] || BUSY_RING) + '</span><div class="job-notice-body"><div class="job-notice-head"><b>' + esc(job.label) + '</b><span class="status-pill status-' + esc(job.status) + '">' + esc(jobStatusText(job)) + '</span></div>' +
      '<small>' + (step ? esc(step.label) + " · " : "") + esc(progressText(job)) + ' · ' + (job.status === "running" ? "rodando há " : "durou ") + duration(job.started_at, job.finished_at) + (job.problems_count ? ' · ' + job.problems_count + ' problema(s)' : '') + '</small>' +
      (job.status === "running" ? '<div class="progress' + (percent == null ? " indeterminate" : "") + '"><i style="width:' + (percent == null ? 30 : percent) + '%"></i></div>' : "") +
      '<small class="job-counts">' + (counts.ok || 0) + ' ok · ' + (counts.copy || 0) + ' copiados · ' + (counts.skip || 0) + ' já existentes · ' + (counts.error || 0) + ' erros</small>' +
      (queueList().length ? '<small class="job-counts">Na fila: ' + queueList().map(function (item) { return esc(item.position + "º " + item.label); }).join(" · ") + '</small>' : "") + '</div>' +
      '<button class="button quiet small" data-open-run="' + esc(job.id) + '">Acompanhar →</button></div>';
  }
  function assignmentRow(item) {
    const overdue = item.status === "atrasada";
    return '<article class="item-row"><span class="item-icon">' + (item.status === "concluida" ? "✓" : overdue ? "!" : "◷") + '</span><div><strong>' + esc(item.title) + '</strong><small>' + esc(item.course) + ' · ' + (item.due_at ? "Prazo " + when(item.due_at, true) : "Sem prazo") + ' · ' + esc(ASSIGNMENT_STATUS[item.status] || item.status) + '</small></div>' + (item.url ? '<a href="' + esc(item.url) + '" target="_blank" rel="noreferrer">Abrir no Canvas →</a>' : "") + '</article>';
  }
  function filteredAssignments() {
    const norm = courseQuery.trim().toLocaleLowerCase("pt-BR");
    return assignmentList().filter(function (item) {
      const matchesCourse = courseFilter === "all" || item.course_key === courseFilter;
      const matchesQuery = !norm || (item.title + " " + item.course).toLocaleLowerCase("pt-BR").includes(norm);
      const matchesStatus = statusFilter === "all" || (statusFilter === "open" ? item.status === "pendente" || item.status === "atrasada" : item.status === statusFilter);
      return matchesCourse && matchesQuery && matchesStatus;
    });
  }
  function assignmentListHtml(items) {
    return items.length ? items.map(assignmentRow).join("") : '<div class="empty"><b>Nenhuma atividade encontrada</b>Sincronize o Canvas ou tente outro termo de busca.<br>' + runButton("update", "Sincronizar agora", "button quiet small") + '</div>';
  }
  function updateCourseList() {
    const items = filteredAssignments();
    const list = document.getElementById("course-list");
    const count = document.getElementById("course-count");
    if (list) { list.innerHTML = assignmentListHtml(items); bindPage(list); }
    if (count) count.textContent = items.length + " atividades";
  }
  function coursesView() {
    const items = filteredAssignments();
    const options = '<option value="all">Todas as matérias</option>' + courseList().map(function (course) { return '<option value="' + esc(course.key) + '"' + (courseFilter === course.key ? " selected" : "") + '>' + esc(course.name) + '</option>'; }).join("");
    return '<header class="page-head"><div><p class="eyebrow">CANVAS · CURSOS E PRAZOS</p><h1>Cursos e atividades</h1><p>Encontre uma tarefa, veja quando vence e abra a página original no Canvas.</p></div><div class="head-actions">' + runButton("assignments", "↻ Atualizar prazos", "button primary") + '</div></header><div data-job-status></div>' +
      '<div class="section-title"><div><h2>Validade local dos cursos</h2><p>O acompanhamento começa na primeira sincronização e dura seis meses. Isso não altera sua matrícula nem o Canvas.</p></div></div>' +
      (courseList().length ? '<section class="grid">' + courseList().map(courseCard).join("") + '</section>' : '<section class="empty"><b>Matérias ainda não sincronizadas</b>Conecte o Canvas e atualize os cursos para começar.</section>') +
      '<div class="section-title"><div><h2>Atividades</h2><p>' + assignmentList().length + ' atividades sincronizadas.</p></div></div>' +
      '<div class="toolbar" id="course-toolbar"><label class="search"><span>⌕</span><input id="course-search" type="search" placeholder="Buscar atividade ou matéria" value="' + esc(courseQuery) + '"></label><select id="course-select" class="select" aria-label="Filtrar por matéria">' + options + '</select><select id="status-select" class="select" aria-label="Filtrar por status">' + STATUS_FILTERS.map(function (pair) { return '<option value="' + pair[0] + '"' + (statusFilter === pair[0] ? " selected" : "") + '>' + pair[1] + '</option>'; }).join("") + '</select><span class="count" id="course-count">' + items.length + ' atividades</span></div>' +
      '<section class="list" id="course-list">' + assignmentListHtml(items) + '</section>';
  }
  function libraryView() {
    const options = '<option value="all"' + (libraryType === "all" ? " selected" : "") + '>Todos os arquivos e itens</option><option value="file"' + (libraryType === "file" ? " selected" : "") + '>Arquivos</option><option value="item"' + (libraryType === "item" ? " selected" : "") + '>Itens e atividades</option>';
    const results = libraryResults.map(function (item) {
      const kind = item.kind === "file" ? "▧" : item.kind === "assignment" ? "◷" : "▤";
      const openLink = item.url ? '<a href="' + esc(item.url) + '" target="_blank" rel="noreferrer">Canvas →</a>' : item.path ? '<a href="/api/file?path=' + encodeURIComponent(item.path) + '" target="_blank" rel="noreferrer">Abrir arquivo →</a>' : "";
      return '<article class="item-row"><span class="item-icon">' + kind + '</span><div><strong>' + esc(item.title) + '</strong><small>' + esc(item.course || "Canvas") + ' · ' + esc(item.kind_label || item.kind) + (item.snippet ? " · " + esc(item.snippet) : "") + '</small></div>' + openLink + '</article>';
    }).join("");
    return '<header class="page-head"><div><p class="eyebrow">BIBLIOTECA LOCAL</p><h1>Arquivos e materiais</h1><p>Pesquise nomes de arquivos, itens dos módulos e atividades sincronizadas.</p></div><div class="head-actions">' + runButton("update", "↓ Sincronizar materiais", "button primary") + '</div></header><div data-job-status></div>' +
      '<div class="notice"><span>i</span><div>Os arquivos são salvos no seu computador. A busca encontra nomes e textos indexados; os resultados podem abrir no Canvas quando houver um link.</div></div>' +
      '<div class="toolbar setup-notice"><label class="search"><span>⌕</span><input id="library-search" type="search" placeholder="Ex.: indução matemática, prova, PDF..." value="' + esc(libraryQuery) + '"></label><select id="library-type" class="select" aria-label="Tipo de resultado">' + options + '</select><span class="count">' + libraryResults.length + ' resultados</span></div>' +
      '<section id="library-results" class="list">' + (libraryQuery ? (results || '<div class="empty">Nenhum resultado para esta busca.</div>') : '<div class="empty"><b>O que você precisa encontrar?</b>Digite um nome, assunto ou palavra-chave para pesquisar nos cursos sincronizados.</div>') + '</section>';
  }
  function assistantView() {
    const bubbles = chatHistory.length ? chatHistory.map(function (message) {
      return '<div class="bubble ' + (message.role === "user" ? "user" : message.role === "system" ? "system" : "assistant") + '">' + esc(message.content) + (message.sources && message.sources.length ? message.sources.map(function (source) { return '<div class="source">' + esc(source.title) + (source.url ? ' · <a href="' + esc(source.url) + '" target="_blank" rel="noreferrer">abrir no Canvas</a>' : source.path ? ' · <a href="/api/file?path=' + encodeURIComponent(source.path) + '" target="_blank" rel="noreferrer">abrir arquivo</a>' : "") + '</div>'; }).join("") : "") + '</div>';
    }).join("") : '<div class="bubble assistant">Olá! Posso procurar atividades, itens dos módulos e arquivos que já foram sincronizados. Experimente perguntar: “Encontre os PDFs sobre somatórios” ou “O que vence esta semana?”</div>';
    return '<header class="page-head"><div><p class="eyebrow">BUSCA COM FERRAMENTAS</p><h1>Assistente de estudos</h1><p>Escolha um modelo. Ele pode pesquisar nos dados locais e indicar de onde veio a informação.</p></div><div class="head-actions"><button class="button quiet" data-go="settings">⚙ Escolher modelo</button></div></header>' +
      '<div class="notice"><span>i</span><div>O assistente pesquisa os dados sincronizados no computador. Ao fazer uma pergunta, a mensagem e os trechos consultados são enviados ao provedor do modelo escolhido. Confira a política de privacidade desse serviço; as ferramentas não alteram o Canvas.</div></div>' +
      '<div class="assistant-layout setup-notice"><section class="panel chat-panel"><div id="chat-messages" class="chat-messages">' + bubbles + '</div><form id="chat-form" class="chat-form"><textarea id="chat-input" required maxlength="2000" placeholder="Pergunte sobre seus cursos, arquivos ou atividades…"></textarea><button class="button primary" type="submit" ' + (pending ? "disabled" : "") + '>Enviar →</button></form></section>' +
      '<aside class="panel"><h2>O que ele pode pesquisar</h2><div class="tool-list"><div class="tool"><b>⌕ Localizar arquivos</b><small>Procura nomes e trechos em materiais de texto sincronizados.</small></div><div class="tool"><b>▤ Encontrar itens Canvas</b><small>Busca atividades, itens de módulos e links dos cursos.</small></div><div class="tool"><b>◷ Consultar prazos</b><small>Usa as datas e os estados que vieram na última sincronização.</small></div></div><p class="meta">Modelo escolhido: ' + esc(activeModelText()) + '</p><button class="button quiet small" data-go="library">Abrir busca direta</button></aside></div>';
  }
  const actions = [
    ["update", "↻", "Sincronizar tudo", "Atualiza cursos, atividades, módulos e arquivos do Canvas."],
    ["assignments", "◷", "Atualizar prazos", "Busca atividades e estados de entrega sem baixar a biblioteca toda."],
    ["prepare", "□", "Preparar exercícios", "Cria uma área local para você praticar e continuar editando."],
    ["solve", "✳", "Gerar rascunhos com IA", "Prepara uma sugestão para você revisar. Nada é enviado ao Canvas."],
    ["summarize", "≡", "Resumir materiais", "Lê PDFs sincronizados e cria resumos locais para revisão."],
    ["export", "↓", "Converter respostas", "Gera arquivos de leitura a partir dos resultados que já existem."]
  ];
  function lastRunOf(action) {
    if (jobs.current && jobs.current.action === action) return jobs.current;
    return (jobs.history || []).find(function (job) { return job.action === action; }) || null;
  }
  function automationView() {
    return '<header class="page-head"><div><p class="eyebrow">TAREFAS AUTOMÁTICAS</p><h1>Automações</h1><p>Escolha uma ação. Cada resultado fica no seu computador para você revisar. Acompanhe o andamento e os problemas em <button class="link-button" data-go="runs">Execuções</button>.</p></div></header>' +
      '<div data-job-status data-show-finished></div>' +
      '<section class="automation-grid setup-notice">' + actions.map(function (action) {
        const needsAi = (action[0] === "solve" || action[0] === "summarize") && !(config && config.llm && config.llm.configured);
        const active = activeEntryFor(action[0]);
        const disabled = pending || Boolean(active) || needsAi;
        const last = lastRunOf(action[0]);
        const lastLine = active && active.status === "queued" ? '<button class="last-run status-queued" data-open-run="' + esc(active.id) + '">⋯ Na fila · posição ' + active.position + '</button>' : last ? '<button class="last-run status-' + esc(last.status) + '" data-open-run="' + esc(last.id) + '">' + (JOB_ICON[last.status] || "") + ' ' + esc(jobStatusText(last)) + ' · ' + esc(ago(last.finished_at || last.started_at)) + '</button>' : '<span class="last-run">Ainda não executada</span>';
        const label = needsAi ? "Configure um modelo" : active ? (active.status === "running" ? "Em execução…" : "Na fila (" + active.position + "º)") : pending ? "Aguarde…" : isRunning() ? "+ Adicionar à fila" : "Executar →";
        return '<article class="card automation"><span class="auto-icon">' + action[1] + '</span><h3>' + action[2] + '</h3><p>' + action[3] + '</p>' + lastLine + '<button class="button ' + (action[0] === "update" ? "primary" : "quiet") + ' small" data-run="' + action[0] + '"' + (disabled ? " disabled" : "") + '>' + label + '</button></article>';
      }).join("") + '</section><div class="notice warn setup-notice"><span>!</span><div>Rascunhos de IA são sugestões para estudo. Revise o conteúdo e siga as regras acadêmicas da sua instituição.</div></div>';
  }
  function allRuns() {
    return (jobs.current ? [jobs.current] : []).concat((jobs.history || []).filter(function (job) { return !jobs.current || job.id !== jobs.current.id; }));
  }
  function queueHtml() {
    const queue = queueList();
    if (!queue.length) return "";
    return '<div class="queue-head"><h3>Na fila (' + queue.length + ')</h3><button class="link-button" data-clear-queue>Limpar fila</button></div>' + queue.map(function (job, index) {
      return '<div class="queue-item' + (job.id === selectedRunId ? " active" : "") + '"><span class="queue-pos">' + job.position + 'º</span><button class="queue-label" data-select-run="' + esc(job.id) + '"><b>' + esc(job.label) + '</b><small>Adicionada ' + esc(ago(job.queued_at)) + '</small></button>' +
        '<span class="queue-actions"><button title="Subir na fila" aria-label="Subir na fila" data-queue-move="' + esc(job.id) + '" data-offset="-1"' + (index === 0 ? " disabled" : "") + '>↑</button><button title="Descer na fila" aria-label="Descer na fila" data-queue-move="' + esc(job.id) + '" data-offset="1"' + (index === queue.length - 1 ? " disabled" : "") + '>↓</button><button class="delete" title="Remover da fila" aria-label="Remover da fila" data-cancel-run="' + esc(job.id) + '">✕</button></span></div>';
    }).join("") + '<div class="queue-head"><h3>Execuções</h3></div>';
  }
  function runListHtml() {
    const runs = allRuns();
    if (!runs.length && !queueList().length) return '<div class="empty"><b>Nenhuma execução ainda</b>Inicie uma automação para acompanhar o andamento aqui.<br><button class="button quiet small" data-go="automation">Ver automações</button></div>';
    return queueHtml() + runs.map(function (job) {
      const percent = progressPercent(job);
      return '<button class="run-item' + (job.id === selectedRunId ? " active" : "") + '" data-select-run="' + esc(job.id) + '"><span class="run-icon status-' + esc(job.status) + '">' + (JOB_ICON[job.status] || "·") + '</span><span class="run-item-body"><b>' + esc(job.label) + '</b><small>' + esc(jobStatusText(job)) + ' · ' + when(job.started_at, true) + ' · ' + duration(job.started_at, job.finished_at) + '</small>' +
        (job.status === "running" ? '<span class="progress' + (percent == null ? " indeterminate" : "") + '"><i style="width:' + (percent == null ? 30 : percent) + '%"></i></span>' : "") + '</span></button>';
    }).join("");
  }
  function runSummaryHtml() {
    const job = runDetail;
    if (!job) return '<div class="empty">Selecione uma execução para ver os detalhes.</div>';
    if (job.status === "queued") {
      const queued = queueList().find(function (item) { return item.id === job.id; });
      if (!queued) return '<div class="empty">Esta automação já saiu da fila. Selecione-a na lista para acompanhar.</div>';
      return '<div class="run-head"><div><p class="eyebrow">' + esc((queued.scope && queued.scope.label || "Todas as matérias").toUpperCase()) + '</p><h2>' + esc(queued.label) + '</h2><p class="meta">Adicionada ' + esc(ago(queued.queued_at)) + ' · começa quando ' + (queued.position === 1 ? "a execução atual terminar" : "as " + (queued.position - 1) + " anteriores terminarem") + '</p></div>' +
        '<div class="run-actions"><span class="status-pill status-queued">⋯ Na fila · ' + queued.position + 'º</span><button class="button quiet small" data-cancel-run="' + esc(queued.id) + '">✕ Remover da fila</button></div></div>';
    }
    const counts = job.counts || {};
    const percent = progressPercent(job);
    const running = job.status === "running";
    const problems = job.problems || [];
    const scope = job.scope && job.scope.label ? job.scope.label : "Todas as matérias";
    const steps = (job.steps || []).map(function (step, index) {
      return '<li class="run-step step-' + esc(step.status) + '"><span>' + (step.status === "done" ? "✓" : step.status === "running" ? BUSY_RING : step.status === "error" ? "✕" : index + 1) + '</span><div><b>' + esc(step.label) + '</b><small>' + esc(STEP_STATUS[step.status] || step.status) + '</small></div></li>';
    }).join("");
    const counters = [["ok", "Concluídos"], ["copy", "Copiados"], ["skip", "Já existentes"], ["info", "Informações"], ["warn", "Avisos"], ["error", "Erros"]].map(function (pair) {
      return '<button class="counter c-' + pair[0] + (logFilter === pair[0] ? " active" : "") + '" data-log-filter="' + pair[0] + '"><strong>' + (counts[pair[0]] || 0) + '</strong><small>' + pair[1] + '</small></button>';
    }).join("");
    const problemList = problems.length ? '<section class="run-problems"><h3>Problemas (' + problems.length + ')</h3>' + problems.slice(0, 50).map(function (problem) {
      return '<div class="problem"><code>' + esc(problem.text) + '</code>' + (problem.hint ? '<small>' + esc(problem.hint) + '</small>' : "") + '</div>';
    }).join("") + (problems.length > 50 ? '<small class="meta">Mostrando 50 de ' + problems.length + '. Use o filtro "Erros" no log para ver todos.</small>' : "") + '</section>' : "";
    const outcome = job.status === "partial" ? '<div class="notice warn"><span>!</span><div>A execução terminou, mas alguns itens não puderam ser baixados. O restante foi salvo normalmente; veja os problemas abaixo.</div></div>'
      : job.status === "error" ? '<div class="notice warn"><span>!</span><div>A execução falhou antes de terminar. Veja os problemas e o fim do log para entender o motivo.</div></div>'
      : job.status === "interrupted" ? '<div class="notice warn"><span>!</span><div>O aplicativo foi fechado durante esta execução. Execute novamente para concluir.</div></div>' : "";
    return '<div class="run-head"><div><p class="eyebrow">' + esc(scope.toUpperCase()) + '</p><h2>' + esc(job.label) + '</h2><p class="meta">Início ' + when(job.started_at, true) + (job.finished_at ? ' · fim ' + when(job.finished_at, true) : '') + ' · ' + (running ? "rodando há " : "duração ") + duration(job.started_at, job.finished_at) + '</p></div>' +
      '<div class="run-actions"><span class="status-pill status-' + esc(job.status) + '">' + (JOB_ICON[job.status] || "") + ' ' + esc(jobStatusText(job)) + '</span>' +
      (running ? '<button class="button quiet small" data-cancel-run="' + esc(job.id) + '">■ Cancelar</button>' : '<button class="button quiet small" data-rerun="' + esc(job.id) + '"' + (pending ? " disabled" : "") + '>' + (isRunning() ? "+ Executar novamente (fila)" : "↻ Executar novamente") + '</button>') + '</div></div>' +
      outcome +
      (running || percent != null ? '<div class="run-progress"><div class="progress' + (percent == null ? " indeterminate" : "") + '"><i style="width:' + (percent == null ? 30 : percent) + '%"></i></div><small>' + esc(progressText(job)) + (percent != null ? " · " + percent + "%" : "") + '</small></div>' : "") +
      (steps ? '<ol class="run-steps">' + steps + '</ol>' : "") +
      '<div class="counters">' + counters + '</div>' + problemList;
  }
  function logLineHtml(line) {
    return '<div class="log-line lvl-' + esc(line.level) + '"><span>' + (line.n + 1) + '</span>' + esc(line.text) + '</div>';
  }
  function visibleLogLines() {
    return runLog.lines.filter(function (line) { return logFilter === "all" || line.level === logFilter; });
  }
  function runsView() {
    const options = LOG_FILTERS.map(function (pair) { return '<option value="' + pair[0] + '"' + (logFilter === pair[0] ? " selected" : "") + '>' + pair[1] + '</option>'; }).join("");
    return '<header class="page-head"><div><p class="eyebrow">ACOMPANHAMENTO</p><h1>Execuções</h1><p>Veja em que etapa cada automação está, o que já foi feito e o que deu errado. As execuções iniciadas pelo terminal também aparecem aqui.</p></div><div class="head-actions"><button class="button quiet" data-go="automation">Iniciar automação</button></div></header>' +
      '<div class="runs-layout"><aside class="panel runs-list" id="runs-list">' + runListHtml() + '</aside>' +
      '<section class="panel run-detail"><div id="run-summary">' + runSummaryHtml() + '</div>' +
      (runDetail ? '<div class="log-toolbar"><h3>Log da execução</h3><select id="log-filter" class="select" aria-label="Filtrar log">' + options + '</select><label class="follow"><input type="checkbox" id="log-follow" checked> Acompanhar o fim</label></div><div id="run-log" class="run-log" role="log" aria-live="off">' + visibleLogLines().map(logLineHtml).join("") + '</div>' : "") +
      '</section></div>';
  }
  function settingsView() {
    const canvas = config && config.canvas || {};
    const llm = config && config.llm || {};
    const selected = providers[llm.provider] ? llm.provider : "openai";
    const modelRows = (config && config.models || []).map(function (model) {
      const active = model.id === llm.selected_model_id;
      return '<div class="provider-row ' + (active ? "active" : "") + '"><i>' + esc((model.provider_label || model.provider || "IA").slice(0, 1).toUpperCase()) + '</i><div><b>' + esc(model.name || model.model) + '</b><small>' + esc(model.provider_label || model.provider) + ' · ' + esc(model.model) + '</small></div>' + (active ? '<span class="badge">Ativo</span>' : '<button data-select-model="' + esc(model.id) + '">Usar</button>') + '<button class="delete" data-remove-model="' + esc(model.id) + '">Remover</button></div>';
    }).join("");
    return '<header class="page-head"><div><p class="eyebrow">CONFIGURAÇÃO ASSISTIDA</p><h1>Conexões e modelos</h1><p>Conecte seu Canvas e escolha o provedor de IA que prefere. Suas chaves são usadas somente pelo serviço local.</p></div></header>' +
      '<div class="notice warn"><span>!</span><div>Use suas próprias credenciais. As chaves ficam salvas no perfil deste computador e não são criptografadas; proteja o acesso à sua conta do dispositivo.</div></div>' +
      '<div class="settings-grid setup-notice"><section class="panel"><h2 class="panel-title">1. Conectar o Canvas</h2><form id="canvas-form" class="form"><div class="field"><label for="canvas-url">Endereço do Canvas da sua instituição</label><input id="canvas-url" name="canvas_base_url" type="url" required placeholder="https://sua-instituicao.instructure.com" value="' + esc(canvas.base_url || "") + '"><small>Copie apenas o endereço principal que aparece no navegador quando você entra no Canvas.</small></div><div class="field"><label for="canvas-token">Token pessoal de acesso</label><input id="canvas-token" name="canvas_token" type="password" autocomplete="new-password" placeholder="' + (canvas.token_configured ? "Token já salvo. Deixe em branco para manter." : "Cole aqui o token pessoal do Canvas") + '"><small>Você cria um token em Configurações → Tokens de acesso. O token é salvo somente no seu computador.</small></div><div class="inline-actions"><button class="button primary" type="submit">Salvar conexão</button><button class="button quiet" type="button" id="test-canvas">Testar acesso</button></div></form></section>' +
      '<section class="panel"><h2 class="panel-title">2. Escolher o modelo de IA</h2><form id="llm-form" class="form"><div class="field"><label for="llm-provider">Provedor</label><select id="llm-provider" name="provider">' + Object.keys(providers).map(function (key) { return '<option value="' + key + '"' + (selected === key ? " selected" : "") + '>' + providers[key].title + '</option>'; }).join("") + '</select></div><div class="field"><label for="llm-model">Nome ou ID do modelo</label><input id="llm-model" name="model" required value="' + esc(llm.model || providers[selected].model) + '" placeholder="Ex.: gpt-6-luna"><small>Para pesquisar seus arquivos, escolha um modelo e servidor com suporte a ferramentas (function calling).</small></div><div class="field"><label for="llm-base">Endereço da API</label><input id="llm-base" name="base_url" type="url" required value="' + esc(llm.base_url || providers[selected].base) + '" placeholder="https://..."></div><div class="field"><label for="llm-key" id="llm-key-label">' + providers[selected].keyLabel + '</label><input id="llm-key" name="api_key" type="password" autocomplete="new-password" placeholder="' + (llm.key_configured ? "Chave já salva. Deixe em branco para manter." : "Cole a chave de API") + '"><small>Para um modelo local, inicie primeiro o servidor no seu computador. Deixe a chave em branco se o servidor não exigir autenticação.</small></div><div class="inline-actions"><button class="button primary" type="submit">Salvar modelo</button><button class="button quiet" type="button" id="test-llm">Testar modelo</button></div></form></section></div>' +
      '<section class="panel setup-notice" style="margin-top:11px"><div class="panel-head"><div><h2>Modelos salvos</h2><p>Você pode guardar mais de uma conexão e trocar o modelo ativo.</p></div><button class="button quiet small" id="save-current-model">＋ Guardar esta conexão</button></div><div class="provider-list">' + (modelRows || '<div class="empty">Ainda não há conexões salvas. Salve o formulário acima para registrar uma opção.</div>') + '</div></section>';
  }
  function render() {
    const pages = { home: homeView, setup: setupView, courses: coursesView, library: libraryView, assistant: assistantView, automation: automationView, runs: runsView, settings: settingsView };
    content.innerHTML = pages[view] ? pages[view]() : homeView();
    bindPage();
    updateJobRegions();
    if (view === "runs") scrollLogToEnd();
  }
  // Atualiza só as áreas de status. O redesenho completo apagava o que o
  // usuário estava digitando a cada atualização do polling.
  function updateJobRegions() {
    const job = jobs.current;
    const running = isRunning();
    if (runChip) {
      runChip.hidden = !running;
      if (running) {
        const percent = progressPercent(job);
        runChip.innerHTML = BUSY_RING + esc(job.label.split(":")[0]) + (percent != null ? " · " + percent + "%" : "") + (queueList().length ? " · +" + queueList().length + " na fila" : "");
        runChip.title = "Acompanhar " + job.label;
      }
    }
    if (runsBadge) {
      runsBadge.hidden = !running;
      runsBadge.textContent = running ? "●" : "";
    }
    content.querySelectorAll("[data-job-status]").forEach(function (region) {
      region.innerHTML = jobStatusBlock(region.hasAttribute("data-show-finished"));
      bindPage(region);
    });
    if (view === "runs") {
      const list = document.getElementById("runs-list");
      if (list) { list.innerHTML = runListHtml(); bindPage(list); }
      const summary = document.getElementById("run-summary");
      if (summary) { summary.innerHTML = runSummaryHtml(); bindPage(summary); }
    }
  }
  function scrollLogToEnd() {
    const log = document.getElementById("run-log");
    const follow = document.getElementById("log-follow");
    if (log && (!follow || follow.checked)) log.scrollTop = log.scrollHeight;
  }
  function appendLogLines(lines) {
    if (!lines.length) return;
    runLog.lines = runLog.lines.concat(lines).slice(-MAX_LOG_LINES);
    const log = document.getElementById("run-log");
    if (!log) return;
    const html = lines.filter(function (line) { return logFilter === "all" || line.level === logFilter; }).map(logLineHtml).join("");
    if (html) log.insertAdjacentHTML("beforeend", html);
    while (log.childElementCount > MAX_LOG_LINES) log.firstElementChild.remove();
    scrollLogToEnd();
  }
  async function loadRunDetail(id) {
    if (!id) { runDetail = null; return; }
    const detail = await request("/jobs/" + encodeURIComponent(id));
    if (selectedRunId !== id) return;
    runDetail = detail;
    if (runLog.id !== id) runLog = { id: id, lines: [], next: 0 };
    let more = true;
    while (more) {
      const chunk = await request("/jobs/" + encodeURIComponent(id) + "/log?since=" + runLog.next);
      if (selectedRunId !== id || runLog.id !== id) return;
      runLog.next = chunk.next;
      appendLogLines(chunk.lines || []);
      more = (chunk.lines || []).length >= 1000;
    }
  }
  function openRun(id) {
    selectedRunId = id;
    runDetail = null;
    runLog = { id: null, lines: [], next: 0 };
    setView("runs");
    loadRunDetail(id).then(function () { if (view === "runs") render(); }).catch(function (error) { toast(error.message); });
  }
  let pollTimer = null;
  let pollBusy = false;
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (document.hidden) return;
    // Rápido durante uma execução; lento em repouso para notar execuções do terminal.
    pollTimer = setTimeout(pollJobs, isRunning() ? 1500 : 15000);
  }
  async function pollJobs() {
    if (pollBusy) return;
    pollBusy = true;
    const wasRunning = isRunning();
    const previousId = jobs.current ? jobs.current.id : null;
    const knownQueue = queueList().map(function (item) { return item.id; });
    try {
      jobs = await request("/jobs");
      const job = jobs.current;
      if (view === "runs" && selectedRunId && (!runDetail || runDetail.status === "running" || runDetail.status === "queued" || (job && job.id === selectedRunId))) {
        await loadRunDetail(selectedRunId);
      }
      updateJobRegions();
      // Com a fila, a execução anterior pode terminar e a próxima já começar
      // entre dois ciclos; procura o resultado da anterior no histórico.
      const previous = wasRunning && previousId ? (job && job.id === previousId ? job : (jobs.history || []).find(function (item) { return item.id === previousId; })) : null;
      const finished = previous && previous.status !== "running" ? previous : null;
      const startedElsewhere = !wasRunning && job && job.status === "running" && job.id !== previousId && knownQueue.indexOf(job.id) < 0;
      if (startedElsewhere) toast("Automação em andamento: " + job.label, "Acompanhar", function () { openRun(job.id); });
      if (finished) {
        const next = isRunning() && job.id !== finished.id ? " Próxima da fila: " + job.label + "." : "";
        toast(finished.label + ": " + jobStatusText(finished).toLowerCase() + "." + next, "Ver detalhes", function () { openRun(finished.id); });
        await refreshData();
      }
    } catch (_) {
      // O próximo ciclo tenta de novo; o aviso de serviço indisponível vem do refresh.
    } finally {
      pollBusy = false;
      schedulePoll();
    }
  }
  function editingForm() {
    const active = document.activeElement;
    return Boolean(active && content.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName));
  }
  async function refreshData() {
    data = await request("/dashboard");
    setConnection();
    // Não redesenha telas com formulário em edição; os dados novos aparecem
    // na próxima navegação.
    if (!editingForm() && view !== "settings" && view !== "setup" && view !== "assistant") render();
  }
  async function refresh() {
    try {
      const results = await Promise.all([request("/dashboard"), request("/config")]);
      data = results[0];
      config = results[1];
      if (data.jobs) jobs = data.jobs;
      if (!initialRouteResolved) {
        if (!config.canvas.configured && !setupDismissed()) view = "setup";
        initialRouteResolved = true;
      }
      setConnection();
      render();
      schedulePoll();
    } catch (error) {
      connection.className = "connection bad";
      connection.innerHTML = "<i></i> Serviço local indisponível";
      content.innerHTML = '<div class="page-head"><div><p class="eyebrow">CANVAS COMPANION</p><h1>Não foi possível abrir o app.</h1><p>Feche esta aba, inicie o executável novamente e abra o endereço local exibido no terminal.</p></div></div><div class="notice warn"><span>!</span><div>' + esc(error.message) + '</div></div><button class="button primary setup-notice" id="retry">Tentar novamente</button>';
      document.getElementById("retry").addEventListener("click", refresh);
    }
  }
  async function runAction(action, params) {
    if (pending) return;
    pending = true;
    const queued = isRunning();
    updateJobRegions();
    content.querySelectorAll("[data-run],[data-rerun]").forEach(function (button) { button.disabled = true; });
    try {
      const result = await post("/actions/" + encodeURIComponent(action), params || {});
      if (result.jobs) jobs = result.jobs;
      const queue = queueList();
      const job = queued && queue.length ? queue[queue.length - 1] : jobs.current;
      toast(result.message || "Ação iniciada.", job ? (queued ? "Ver fila" : "Acompanhar") : null, job ? function () { openRun(job.id); } : null);
    } catch (error) { toast(error.message); }
    finally {
      pending = false;
      if (view === "runs" && jobs.current && !queued) { selectedRunId = jobs.current.id; runDetail = null; runLog = { id: null, lines: [], next: 0 }; }
      render();
      schedulePoll();
    }
  }
  function rerun(id) {
    const job = allRuns().find(function (item) { return item.id === id; }) || runDetail;
    if (!job) return;
    const params = job.params || {};
    runAction(job.action, { course: params.course || undefined, assignment_id: params.assignment_id || undefined, force: Boolean(params.force) });
  }
  async function queueAction(path, payload) {
    try {
      const result = await post(path, payload || {});
      if (result.jobs) jobs = result.jobs;
      updateJobRegions();
      if (view === "automation") render();
      return result;
    } catch (error) { toast(error.message); return null; }
  }
  async function cancelRun(id) {
    const inQueue = queueList().some(function (item) { return item.id === id; });
    if (inQueue) {
      const result = await queueAction("/jobs/" + encodeURIComponent(id) + "/cancel");
      if (result) toast(result.message);
      if (selectedRunId === id) { selectedRunId = null; runDetail = null; if (view === "runs") render(); }
      return;
    }
    if (!window.confirm("Cancelar esta execução? O que já foi baixado ou gerado continua salvo. Se houver itens na fila, o próximo começa em seguida.")) return;
    try {
      const result = await post("/jobs/" + encodeURIComponent(id) + "/cancel", {});
      if (result.jobs) jobs = result.jobs;
      toast(result.message || "Cancelamento solicitado.");
      updateJobRegions();
    } catch (error) { toast(error.message); }
  }
  async function searchLibrary() {
    const input = document.getElementById("library-search");
    if (!input) return;
    libraryQuery = input.value;
    const type = document.getElementById("library-type");
    libraryType = type ? type.value : libraryType;
    if (!libraryQuery.trim()) { libraryResults = []; render(); return; }
    try {
      const response = await request("/search?q=" + encodeURIComponent(libraryQuery) + "&type=" + encodeURIComponent(libraryType));
      libraryResults = response.results || [];
      render();
      const restored = document.getElementById("library-search");
      if (restored) { restored.focus(); restored.setSelectionRange(libraryQuery.length, libraryQuery.length); }
    } catch (error) { toast(error.message); }
  }
  async function sendChat(event) {
    event.preventDefault();
    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message || pending) return;
    chatHistory.push({ role: "user", content: message });
    pending = true;
    render();
    try {
      const result = await post("/chat", { message: message, history: chatHistory.slice(-12, -1) });
      chatHistory.push({ role: "assistant", content: result.answer || "Não recebi uma resposta.", sources: result.sources || [] });
    } catch (error) { chatHistory.push({ role: "system", content: error.message }); }
    pending = false;
    render();
    const messages = document.getElementById("chat-messages");
    messages.scrollTop = messages.scrollHeight;
  }
  async function exitApplication() {
    if (!window.confirm("Encerrar o Canvas Companion? A janela local será fechada.")) return;
    const button = document.getElementById("exit-app");
    if (button) button.disabled = true;
    try {
      await post("/shutdown", {});
      clearTimeout(pollTimer);
      pollTimer = null;
      if (libraryTimer) { clearTimeout(libraryTimer); libraryTimer = null; }
      connection.className = "connection";
      connection.innerHTML = "<i></i>Aplicativo encerrado";
      content.innerHTML = '<section class="app-closed"><span>✓</span><h1>Canvas Companion encerrado</h1><p>O servidor local foi fechado. Você pode fechar esta aba.</p></section>';
    } catch (error) {
      if (button) button.disabled = false;
      toast("Não foi possível encerrar o aplicativo: " + error.message);
    }
  }
  function bindPage(root) {
    const scope = root || content;
    scope.querySelectorAll("[data-go]").forEach(function (button) { button.addEventListener("click", function () { setView(button.dataset.go); }); });
    scope.querySelectorAll("[data-run]").forEach(function (button) { button.addEventListener("click", function () { runAction(button.dataset.run); }); });
    // "N pendentes" leva direto à lista da matéria, já filtrada, e não ao topo.
    scope.querySelectorAll("[data-course-filter]").forEach(function (button) { button.addEventListener("click", function () { courseFilter = button.dataset.courseFilter; statusFilter = "open"; courseQuery = ""; setView("courses", "course-toolbar"); }); });
    scope.querySelectorAll("[data-open-run]").forEach(function (button) { button.addEventListener("click", function () { openRun(button.dataset.openRun); }); });
    scope.querySelectorAll("[data-select-run]").forEach(function (button) { button.addEventListener("click", function () { openRun(button.dataset.selectRun); }); });
    scope.querySelectorAll("[data-cancel-run]").forEach(function (button) { button.addEventListener("click", function () { cancelRun(button.dataset.cancelRun); }); });
    scope.querySelectorAll("[data-rerun]").forEach(function (button) { button.addEventListener("click", function () { rerun(button.dataset.rerun); }); });
    scope.querySelectorAll("[data-queue-move]").forEach(function (button) { button.addEventListener("click", function () { queueAction("/jobs/" + encodeURIComponent(button.dataset.queueMove) + "/move", { offset: Number(button.dataset.offset) }); }); });
    scope.querySelectorAll("[data-clear-queue]").forEach(function (button) { button.addEventListener("click", function () { if (window.confirm("Remover todas as automações da fila? A execução atual continua.")) queueAction("/queue/clear").then(function (result) { if (result) toast(result.message); }); }); });
    scope.querySelectorAll("[data-log-filter]").forEach(function (button) { button.addEventListener("click", function () { setLogFilter(logFilter === button.dataset.logFilter ? "all" : button.dataset.logFilter); }); });
    if (root) return;
    const lf = document.getElementById("log-filter");
    if (lf) lf.addEventListener("change", function () { setLogFilter(lf.value); });
    const cs = document.getElementById("course-search");
    if (cs) cs.addEventListener("input", function () { courseQuery = cs.value; updateCourseList(); });
    const cf = document.getElementById("course-select");
    if (cf) cf.addEventListener("change", function () { courseFilter = cf.value; updateCourseList(); });
    const sf = document.getElementById("status-select");
    if (sf) sf.addEventListener("change", function () { statusFilter = sf.value; updateCourseList(); });
    const ls = document.getElementById("library-search");
    if (ls) {
      ls.addEventListener("input", function () { clearTimeout(libraryTimer); libraryQuery = ls.value; libraryTimer = setTimeout(searchLibrary, 350); });
      ls.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); clearTimeout(libraryTimer); searchLibrary(); } });
    }
    const lt = document.getElementById("library-type");
    if (lt) lt.addEventListener("change", searchLibrary);
    const chatForm = document.getElementById("chat-form");
    if (chatForm) chatForm.addEventListener("submit", sendChat);
    const provider = document.getElementById("llm-provider");
    if (provider) provider.addEventListener("change", function () {
      const preset = providers[provider.value];
      document.getElementById("llm-key-label").textContent = preset.keyLabel;
      document.getElementById("llm-base").value = preset.base;
      document.getElementById("llm-model").value = preset.model;
    });
    const canvasForm = document.getElementById("canvas-form");
    if (canvasForm) canvasForm.addEventListener("submit", saveCanvas);
    const firstCanvasForm = document.getElementById("first-canvas-form");
    if (firstCanvasForm) firstCanvasForm.addEventListener("submit", completeFirstCanvasSetup);
    content.querySelectorAll("[data-dismiss-setup]").forEach(function (button) { button.addEventListener("click", dismissCanvasSetup); });
    const llmForm = document.getElementById("llm-form");
    if (llmForm) llmForm.addEventListener("submit", saveModel);
    const testCanvas = document.getElementById("test-canvas");
    if (testCanvas) testCanvas.addEventListener("click", testCanvasConnection);
    const testLlm = document.getElementById("test-llm");
    if (testLlm) testLlm.addEventListener("click", testModelConnection);
    const saveModelButton = document.getElementById("save-current-model");
    if (saveModelButton) saveModelButton.addEventListener("click", saveCurrentModel);
    content.querySelectorAll("[data-select-model]").forEach(function (button) { button.addEventListener("click", function () { post("/models/select", { id: button.dataset.selectModel }).then(function () { return refresh(); }).then(function () { toast("Modelo ativo atualizado."); }).catch(function (error) { toast(error.message); }); }); });
    content.querySelectorAll("[data-remove-model]").forEach(function (button) { button.addEventListener("click", function () { removeModel(button.dataset.removeModel); }); });
  }
  function setLogFilter(value) {
    logFilter = value;
    const log = document.getElementById("run-log");
    if (log) log.innerHTML = visibleLogLines().map(logLineHtml).join("");
    const select = document.getElementById("log-filter");
    if (select) select.value = value;
    const summary = document.getElementById("run-summary");
    if (summary) { summary.innerHTML = runSummaryHtml(); bindPage(summary); }
    scrollLogToEnd();
  }
  async function saveCanvas(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = { canvas_base_url: String(form.get("canvas_base_url") || "").trim(), canvas_token: String(form.get("canvas_token") || "").trim() };
    try { await post("/config", payload); await refresh(); toast("Conexão Canvas salva neste computador."); }
    catch (error) { toast(error.message); }
  }
  async function completeFirstCanvasSetup(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const payload = {
      canvas_base_url: String(values.get("canvas_base_url") || "").trim(),
      canvas_token: String(values.get("canvas_token") || "").trim()
    };
    if (!payload.canvas_token) { toast("Cole o token pessoal do Canvas para continuar."); return; }
    const submit = form.querySelector('button[type="submit"]');
    if (submit) { submit.disabled = true; submit.textContent = "Conectando…"; }
    try {
      config = await post("/config", payload);
      const result = await post("/test-canvas", {});
      try { localStorage.setItem(SETUP_DISMISSED_KEY, "1"); } catch (_) {}
      view = "home";
      await refresh();
      toast(result.message || "Canvas conectado.");
    } catch (error) {
      toast(error.message);
    } finally {
      if (submit && document.body.contains(submit)) { submit.disabled = false; submit.textContent = "Salvar e testar conexão"; }
    }
  }
  async function saveModel(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = { provider: String(form.get("provider")), model: String(form.get("model") || "").trim(), base_url: String(form.get("base_url") || "").trim(), api_key: String(form.get("api_key") || "").trim() };
    try { await post("/config", payload); await refresh(); toast("Configuração de IA salva."); }
    catch (error) { toast(error.message); }
  }
  async function testCanvasConnection() {
    try { await saveCanvasFromForm(); const result = await post("/test-canvas", {}); toast(result.message || "Canvas conectado."); await refresh(); }
    catch (error) { toast(error.message); }
  }
  async function saveCanvasFromForm() {
    const form = document.getElementById("canvas-form");
    const values = new FormData(form);
    await post("/config", { canvas_base_url: String(values.get("canvas_base_url") || "").trim(), canvas_token: String(values.get("canvas_token") || "").trim() });
  }
  async function testModelConnection() {
    try { await saveModelFromForm(); const result = await post("/test-llm", {}); toast(result.message || "Modelo respondeu."); await refresh(); }
    catch (error) { toast(error.message); }
  }
  async function saveModelFromForm() {
    const form = document.getElementById("llm-form");
    const values = new FormData(form);
    await post("/config", { provider: String(values.get("provider")), model: String(values.get("model") || "").trim(), base_url: String(values.get("base_url") || "").trim(), api_key: String(values.get("api_key") || "").trim() });
  }
  async function saveCurrentModel() {
    try {
      await saveModelFromForm();
      const result = await post("/models/save-current", {});
      await refresh();
      toast(result.message || "Conexão guardada.");
    } catch (error) { toast(error.message); }
  }
  async function removeModel(id) {
    if (!window.confirm("Remover esta conexão salva?")) return;
    try { await post("/models/remove", { id: id }); await refresh(); toast("Conexão removida."); }
    catch (error) { toast(error.message); }
  }
  function openHelp() {
    modal.innerHTML = '<div class="modal-backdrop" data-dismiss><section class="modal"><header class="modal-head"><div><h2>Privacidade e segurança</h2><p>O Canvas Companion roda neste computador.</p></div></header><div class="modal-body"><div class="notice"><span>i</span><div>Tokens do Canvas e chaves de IA ficam salvos no perfil local do usuário. Os trechos necessários dos materiais podem ser enviados ao provedor de IA escolhido durante uma pergunta ou resumo.</div></div><div class="modal-actions"><button class="button primary small" data-dismiss>Entendi</button></div></div></section></div>';
    modal.querySelectorAll("[data-dismiss]").forEach(function (item) { item.addEventListener("click", function () { modal.innerHTML = ""; }); });
  }
  themeButton.addEventListener("click", function () {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  });
  document.querySelectorAll("[data-view]").forEach(function (button) { button.addEventListener("click", function (event) { event.preventDefault(); setView(button.dataset.view); }); });
  document.getElementById("help").addEventListener("click", openHelp);
  document.getElementById("exit-app").addEventListener("click", exitApplication);
  if (runChip) runChip.addEventListener("click", function () { if (jobs.current) openRun(jobs.current.id); });
  // Sem polling com a aba escondida; ao voltar, atualiza na hora.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { clearTimeout(pollTimer); return; }
    if (initialRouteResolved) pollJobs();
  });
  applyTheme(document.documentElement.dataset.theme || "light", false);
  refresh();
})();
