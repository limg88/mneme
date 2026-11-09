"use strict";

const $ = (id) => document.getElementById(id);
let bearer = "", pending = false, context = [], evidenceIds = [], epoch = 0;
let cloudEnabled = false, quotaAvailable = true, loaded = false;
const labels = {decision: "Decisione documentata", proposal: "Proposta", context: "Contesto", uncertain: "Da chiarire"};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function error(message) {
  $("error").textContent = message;
  $("error").classList.toggle("hidden", !message);
}
async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (bearer) headers.Authorization = "Bearer " + bearer;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers, credentials: "same-origin", cache: "no-store"});
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 401) $("login").classList.remove("hidden");
    const message = typeof body.detail === "string" ? body.detail : "Richiesta non valida. Controlla la domanda e riprova.";
    throw new Error(response.status === 401 ? "Collega la memoria per iniziare." : message);
  }
  return body;
}
function date(value) {
  if (!value) return "non disponibile";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("it-IT", {dateStyle:"short", timeStyle:"short"});
}
function setBusy(value) {
  pending = value;
  $("send").disabled = value || !cloudEnabled || !quotaAvailable;
  $("project").disabled = value;
  $("question").disabled = value;
  document.querySelectorAll(".example, .followups button").forEach(b => b.disabled = value);
}
async function loadInfo() {
  try {
    const info = await api("/v1/chat/info");
    loaded = true;
    cloudEnabled = info.enabled;
    $("login").classList.add("hidden");
    $("connection").replaceChildren(element("i"), document.createTextNode(info.enabled ? "Memoria collegata" : "Chat disabilitata"));
    $("connection").classList.toggle("offline", !info.enabled);
    const current = $("project").value;
    $("project").replaceChildren(new Option("Tutti i progetti", ""), ...info.projects.map(p => new Option(p, p)));
    $("project").value = current;
    $("freshness").textContent = info.last_sync ? "Ultimo ciclo: " + date(info.last_sync) : "Nessuna sincronizzazione registrata";
    let notice = "Haiku aziendale · Ogni domanda consuma quota del piano · Copertura parziale";
    const spending = info.spending;
    quotaAvailable = spending.can_start_call !== false;
    if (spending.daily_remaining !== undefined) {
      notice += ` · Residuo stimato: $${Math.min(spending.daily_remaining, spending.total_remaining).toFixed(2)}`;
      if (!spending.can_start_call) notice += " · Budget esaurito";
    }
    if (!info.enabled) notice = "Chat disabilitata. Avvia work-memory serve --allow-cloud --open per usare Haiku.";
    else if (!quotaAvailable) notice = "Budget cloud giornaliero o totale insufficiente. Nessuna nuova chiamata: limiti e consumi restano invariati.";
    $("notice").textContent = notice;
    setBusy(pending);
  } catch (err) {
    $("connection").replaceChildren(element("i"), document.createTextNode("Da collegare"));
    $("connection").classList.add("offline");
    $("send").disabled = true;
    error(err.message);
  }
}
function citations(container, refs, sources) {
  for (const ref of refs) {
    const source = sources.find(s => s.id === ref.source);
    if (!source) continue;
    const button = element("button", `${ref.source.slice(1)} · ${source.title}`, "citation");
    button.type = "button";
    button.title = "Apri il passaggio citato";
    button.addEventListener("click", () => openEvidence(source, ref.quote));
    container.append(button);
  }
}
async function openEvidence(source, quote) {
  const dialog = $("evidence-dialog");
  $("evidence-title").textContent = source.title;
  $("evidence-meta").textContent = "Verifica dell’accesso alla fonte…";
  $("evidence-content").replaceChildren();
  if (!dialog.open) dialog.showModal();
  try {
    const observation = await api("/v1/observations/" + encodeURIComponent(source.observation_id));
    if (!dialog.open) return;
    const start = observation.content.indexOf(quote, source.start_offset || 0);
    if (start < 0) throw new Error("Il passaggio non è più verificabile nella fonte.");
    $("evidence-meta").textContent = `${source.source.toUpperCase()} / ${source.project} · Acquisito ${date(observation.observed_at)} · Aggiornato alla fonte ${date(observation.source_updated_at)}`;
    const content = $("evidence-content");
    content.append(element("blockquote", quote, "source-quote"));
    const url = new URL(observation.source_url, window.location.origin);
    if (["https:", "http:"].includes(url.protocol) && /^https?:\/\//i.test(observation.source_url)) {
      const link = element("a", "Apri la fonte originale ↗", "source-link");
      link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
      content.append(link);
    }
    content.append(element("p", "Copertura: " + source.coverage + ". La citazione esatta non certifica da sola l’interpretazione.", "source-note"));
    content.append(element("p", "IL PASSAGGIO NEL SUO CONTESTO", "context-label"));
    const paragraph = element("p", undefined, "source-context");
    paragraph.append(document.createTextNode(observation.content.slice(Math.max(0, start - 700), start)), element("mark", quote), document.createTextNode(observation.content.slice(start + quote.length, start + quote.length + 1000)));
    content.append(paragraph);
  } catch (err) {
    $("evidence-meta").textContent = "Fonte non disponibile";
    $("evidence-content").replaceChildren(element("p", err.message, "error"));
  }
}
function renderAnswer(turn, answer) {
  const head = element("div", undefined, "answer-head");
  head.append(element("span", "m·", "answer-mark"), element("span", "DALLA TUA MEMORIA"));
  turn.append(head);
  for (const claim of answer.claims) {
    const block = element("div", undefined, "claim");
    block.append(element("span", labels[claim.kind] || "Contesto", "badge " + claim.kind), element("p", claim.text));
    citations(block, claim.citations, answer.sources);
    turn.append(block);
  }
  if (answer.timeline.length) {
    const timeline = element("section", undefined, "timeline");
    timeline.append(element("div", "IL FILO NEL TEMPO", "section-label"));
    for (const event of answer.timeline) {
      const entry = element("div", undefined, "timeline-event");
      entry.append(element("time", event.date || "Data non documentata"), element("p", event.label));
      citations(entry, event.citations, answer.sources);
      timeline.append(entry);
    }
    turn.append(timeline);
  }
  if (answer.gaps.length) {
    const gaps = element("div", undefined, "gaps");
    gaps.append(element("span", "COSA RESTA DA CHIARIRE", "badge context"));
    for (const gap of answer.gaps) gaps.append(element("p", gap));
    turn.append(gaps);
  }
  const details = element("details", undefined, "answer-details");
  details.append(element("summary", `${answer.sources.length} ${answer.sources.length === 1 ? "fonte citata" : "fonti citate"} · Copertura e limiti`));
  for (const limit of answer.limitations) details.append(element("p", limit));
  turn.append(details);
  if (answer.sources.length) {
    const followups = element("div", undefined, "followups");
    for (const text of ["Chi l’ha deciso?", "E prima cosa era previsto?", "Quali punti restano da confermare?"]) {
      const button = element("button", text + " ↗");
      button.type = "button";
      button.addEventListener("click", () => { if (!pending) { $("question").value = text; $("question-form").requestSubmit(); } });
      followups.append(button);
    }
    turn.append(followups);
  }
}
$("question-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pending || !loaded || !cloudEnabled || !quotaAvailable) return;
  const question = $("question").value.trim();
  if (!question) return;
  error("");
  const requestEpoch = epoch;
  const turn = element("article", undefined, "turn");
  const heading = element("div", undefined, "question-heading");
  heading.append(element("span", "↗", "user-icon"), element("h2", question));
  turn.append(heading);
  const loading = element("div", undefined, "loading");
  const loadingText = element("div", "Cerco le evidenze e ricostruisco la risposta…");
  loadingText.append(element("small", "Haiku sta consultando la memoria. Può richiedere qualche istante."));
  loading.append(element("span", undefined, "pulse"), loadingText); turn.append(loading);
  $("conversation").append(turn);
  $("welcome").classList.add("hidden"); document.body.classList.add("has-chat");
  setBusy(true);
  turn.scrollIntoView({behavior:"smooth", block:"start"});
  try {
    const answer = await api("/v1/chat", {method:"POST", body:JSON.stringify({question, project:$("project").value || null, previous_questions:context, evidence_ids:evidenceIds})});
    if (requestEpoch !== epoch) return;
    loading.remove(); renderAnswer(turn, answer);
    context = [...context, question].slice(-4);
    evidenceIds = [...new Set(answer.sources.map(s => s.observation_id))].slice(0, 8);
    $("question").value = "";
    $("question").placeholder = "Approfondisci questa decisione…";
  } catch (err) {
    if (requestEpoch !== epoch) return;
    loading.remove();
    turn.append(element("p", err.message, "error"));
    turn.append(element("p", "La domanda è rimasta nel campo: puoi modificarla e riprovare. Un nuovo invio può consumare altra quota.", "source-note"));
  } finally {
    setBusy(false); await loadInfo();
    $("question").focus();
  }
});
$("question").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("question-form").requestSubmit(); }
});
document.querySelectorAll(".example").forEach(button => button.addEventListener("click", () => { $("question").value = button.dataset.question; $("question").focus(); }));
function reset() {
  epoch++; context = []; evidenceIds = [];
  $("conversation").replaceChildren(); $("welcome").classList.remove("hidden"); document.body.classList.remove("has-chat");
  $("question").value = ""; $("question").placeholder = "Cosa abbiamo deciso su…"; error("");
  if (!pending) $("question").focus();
}
$("new-chat").addEventListener("click", reset);
$("project").addEventListener("change", reset);
$("close-evidence").addEventListener("click", () => $("evidence-dialog").close());
$("evidence-dialog").addEventListener("click", event => { if (event.target === $("evidence-dialog")) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right) event.target.close(); } });
$("login").addEventListener("submit", async event => { event.preventDefault(); bearer = $("token").value.trim(); $("token").value = ""; error(""); await loadInfo(); });
loadInfo();
