/* Ο Δικηγόρος σου — frontend χωρίς build step. Οι συζητήσεις μένουν στον browser (localStorage). */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const EXAMPLES = [
  ["Εργατικό", "Ποια προθεσμία προειδοποίησης απαιτείται για την καταγγελία σύμβασης εργασίας αορίστου χρόνου και τι αποζημίωση οφείλεται χωρίς προειδοποίηση;"],
  ["Αστικό", "Πότε παραγράφεται η αξίωση από αδικοπραξία και ποιες αξιώσεις υπάγονται στην πενταετή παραγραφή;"],
  ["Ποινικό", "Ποια είναι η ποινή για την ανθρωποκτονία από αμέλεια και πώς διαφέρει από τη σωματική βλάβη από αμέλεια;"],
  ["Προσωπικά δεδομένα", "Ποια ένδικα βοηθήματα έχει το υποκείμενο των δεδομένων κατά του υπευθύνου επεξεργασίας κατά τον ν. 4624/2019;"],
];

const S = {
  meta: null, chats: [], current: null, mode: "ask", kinds: new Set(), acts: new Set(),
  busy: false, code: "", abort: null,
};

/* ------------------------------------------------------------ αποθήκευση */

function load() {
  try {
    S.chats = JSON.parse(localStorage.getItem("dik.chats") || "[]");
    S.code = localStorage.getItem("dik.code") || "";
  } catch { S.chats = []; }
}
function save() {
  try { localStorage.setItem("dik.chats", JSON.stringify(S.chats.slice(0, 60))); } catch { /* γεμάτο ή απενεργοποιημένο */ }
}

/* ------------------------------------------------------------ API */

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (S.code) headers["X-Access-Code"] = encodeURIComponent(S.code);
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) { await askCode(); return api(path, options); }
  return resp;
}
async function apiJson(path, options) {
  const resp = await api(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || data.detail?.[0]?.msg || `Σφάλμα ${resp.status}`);
  return data;
}
function askCode() {
  return new Promise((resolve) => {
    const dialog = $("#login-dialog"), form = $("#login-form"), error = $("#login-error");
    if (!dialog.open) dialog.showModal();
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const code = $("#access-code").value;
      const resp = await fetch("/api/login", { method: "POST", headers: { "X-Access-Code": encodeURIComponent(code) } });
      if (resp.ok) {
        S.code = code;
        try { localStorage.setItem("dik.code", code); } catch { /* χωρίς αποθήκευση */ }
        error.hidden = true; dialog.close(); resolve();
      } else { error.textContent = "Ο κωδικός δεν είναι σωστός."; error.hidden = false; }
    };
  });
}

/* ------------------------------------------------------------ markdown + παραπομπές */

function citeChips(inner, sources) {
  const byN = new Map((sources || []).map((s) => [s.n, s]));
  const chip = (n, pin) => {
    const src = byN.get(n);
    if (!src || src.attached === false) {
      return `<span class="cite is-bad" title="Παραπομπή σε έγγραφο που δεν δόθηκε στο μοντέλο">[${n}]</span>`;
    }
    const label = esc(src.cite) + (pin ? ", " + pin : "");
    return `<button type="button" class="cite" data-uid="${esc(src.uid)}" title="${esc(src.heading || src.label)}">${label}</button>`;
  };
  if (/^\d{1,3}(\s*[,;]\s*\d{1,3})*$/.test(inner)) return inner.match(/\d+/g).map((n) => chip(+n, "")).join("");
  const pinned = inner.match(/^(\d{1,3})\s*[,›:·]\s*(.+)$/);
  if (pinned) return chip(+pinned[1], pinned[2].trim());
  return null;
}

function inline(text, sources) {
  let out = esc(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[\s(«])\*([^*\s][^*]*)\*(?=[\s).,;·»]|$)/g, "$1<em>$2</em>");
  out = out.replace(/\[([^\[\]\n]{1,80})\]/g, (whole, inner) => citeChips(inner.replace(/&gt;/g, "›"), sources) ?? whole);
  return out;
}

function markdown(text, sources) {
  const html = [];
  let para = [], list = null;
  const flushPara = () => { if (para.length) { html.push(`<p>${para.join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { html.push(`</${list}>`); list = null; } };
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); flushList();
      const level = Math.min(3, m[1].length);
      html.push(`<h${level}>${inline(m[2], sources)}</h${level}>`);
    } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      flushPara(); flushList(); html.push("<hr>");
    } else if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
      flushPara();
      if (list !== "ul") { flushList(); html.push("<ul>"); list = "ul"; }
      html.push(`<li>${inline(m[1], sources)}</li>`);
    } else if ((m = line.match(/^\s*(\d{1,3})[.)]\s+(.*)$/))) {
      flushPara();
      if (list !== "ol") { flushList(); html.push("<ol>"); list = "ol"; }
      html.push(`<li value="${m[1]}">${inline(m[2], sources)}</li>`);
    } else if ((m = line.match(/^>\s?(.*)$/))) {
      flushPara(); flushList(); html.push(`<blockquote>${inline(m[1], sources)}</blockquote>`);
    } else {
      flushList(); para.push(inline(line, sources));
    }
  }
  flushPara(); flushList();
  return html.join("");
}

/** Η απάντηση ως απλό κείμενο, με τις παραπομπές γραμμένες («(ΑΚ 914, παρ. 2)») αντί για αριθμούς. */
function plainAnswer(msg) {
  const byN = new Map((msg.sources || []).map((s) => [s.n, s]));
  return String(msg.content || "").replace(/\[([^\[\]\n]{1,80})\]/g, (whole, inner) => {
    if (/^\d{1,3}(\s*[,;]\s*\d{1,3})*$/.test(inner)) {
      const names = inner.match(/\d+/g).map((n) => byN.get(+n)?.cite).filter(Boolean);
      return names.length ? `(${names.join(", ")})` : whole;
    }
    const pinned = inner.match(/^(\d{1,3})\s*[,›>:·]\s*(.+)$/);
    const src = pinned && byN.get(+pinned[1]);
    return src ? `(${src.cite}, ${pinned[2].trim()})` : whole;
  });
}

/* ------------------------------------------------------------ απόδοση */

function badges(src) {
  const out = [];
  const act = src.act || {};
  if (src.not_in_force) out.push(`<span class="badge badge-ocr">δεν ίσχυε στις ${esc(src.not_in_force)}</span>`);
  else if (src.as_of) out.push(`<span class="badge badge-date">όπως ίσχυε στις ${esc(src.as_of)}</span>`);
  else if (act.consolidation === "enacted") out.push(`<span class="badge badge-old" title="${esc(act.note)}">όπως εκδόθηκε${act.in_force ? " " + esc(act.in_force.slice(0, 4)) : ""}</span>`);
  else if (act.consolidation === "amended") out.push(`<span class="badge badge-ok" title="${esc(act.note)}">με τροποποιήσεις</span>`);
  else if (act.consolidation === "consolidated") out.push(`<span class="badge badge-ok" title="${esc(act.note)}">κωδικοποίηση</span>`);
  if (src.ocr) out.push(`<span class="badge badge-ocr" title="Κείμενο από οπτική αναγνώριση σαρωμένου ΦΕΚ· πιθανά λάθη">OCR</span>`);
  return `<span class="badges">${out.join("")}</span>`;
}

function botHtml(msg) {
  const parts = [];
  if (msg.pending) parts.push(`<div class="steps"><span class="dot"></span><span>${esc(msg.status || "Επεξεργασία")}…</span></div>`);
  if (msg.plan && (msg.plan.searches?.length || msg.plan.citations?.length)) {
    const searches = msg.plan.searches.map((s) => {
      const scope = [...(s.acts || []), ...(s.kinds || [])].join(", ");
      return `<li>${esc(s.q)}${scope ? ` <span class="muted">(${esc(scope)})</span>` : ""}</li>`;
    }).join("");
    const cites = (msg.plan.citations || []).map((c) => `${esc(c.act)} ${esc(c.article)}`).join(" · ");
    parts.push(`<details class="plan"><summary>Πώς έγινε η αναζήτηση</summary><ul>${searches}</ul>${cites ? `<div>Άμεση ανάσυρση: ${cites}</div>` : ""}</details>`);
  }
  if (msg.kind === "search") {
    const hits = (msg.hits || []).map((h) => `<button type="button" class="hit" data-uid="${esc(h.uid)}">
      <span class="cite-line">${esc(h.cite)}</span>${badges(h)}
      <div class="head-line">${esc(h.heading || h.act.title)}</div>
      <div class="snippet">${esc(h.snippet)}</div></button>`).join("");
    parts.push(hits || (msg.pending ? "" : `<p class="muted">Δεν βρέθηκαν διατάξεις με αυτούς τους όρους.</p>`));
  } else if (msg.content) {
    parts.push(`<div class="answer${msg.pending ? " cursor" : ""}">${markdown(msg.content, msg.sources)}</div>`);
  }
  if (msg.error) parts.push(`<div class="notice notice-bad">${esc(msg.error)}</div>`);
  for (const w of msg.warnings || []) parts.push(`<div class="notice notice-warn">${esc(w)}</div>`);
  const check = msg.check;
  if (check) {
    if (check.invalid?.length) parts.push(`<div class="notice notice-bad">Η απάντηση παραπέμπει σε έγγραφο που δεν της δόθηκε (${check.invalid.map((n) => `[${n}]`).join(", ")}). Αντιμετωπίστε το σχετικό σημείο ως ατεκμηρίωτο.</div>`);
    if (check.unverified_pins?.length) parts.push(`<div class="notice notice-warn">Δεν επιβεβαιώθηκε αυτόματα ο εντοπισμός παραγράφου σε ${check.unverified_pins.length === 1 ? "μία παραπομπή" : check.unverified_pins.length + " παραπομπές"}· ελέγξτε τον στο κείμενο του άρθρου.</div>`);
    if (check.no_citations) parts.push(`<div class="notice notice-warn">Η απάντηση δεν περιέχει παραπομπές στις πηγές της. Ελέγξτε την απέναντι στα άρθρα παρακάτω.</div>`);
  }
  if (msg.sources?.length) {
    const cited = new Set(check?.cited || []);
    const rows = msg.sources.map((s) => `<button type="button" class="source${check && !cited.has(s.n) ? " is-unused" : ""}" data-uid="${esc(s.uid)}">
      <span class="n">${s.n}</span>
      <span><span class="cite-line">${esc(s.cite)}</span>${badges(s)}
      <div class="head-line">${esc(s.heading || s.act.title)}</div></span></button>`).join("");
    parts.push(`<div class="sources"><div class="sources-head"><h3>Πηγές (${msg.sources.length})</h3></div><div class="source-list">${rows}</div></div>`);
  }
  if (!msg.pending && msg.kind !== "search" && msg.content) {
    parts.push(`<div class="msg-actions"><button type="button" class="btn btn-small" data-copy>Αντιγραφή απάντησης</button></div>`);
  }
  return `<div class="card">${parts.join("")}</div>`;
}

function renderThread() {
  const thread = $("#thread");
  thread.replaceChildren();
  const chat = S.current;
  $("#chat-title").textContent = chat?.title || "Νέα συζήτηση";
  if (!chat || !chat.messages.length) { thread.append(welcome()); return; }
  chat.messages.forEach((msg, i) => thread.append(messageNode(msg, i)));
  thread.scrollTop = thread.scrollHeight;
}

function messageNode(msg, index) {
  const node = el("div", `msg msg-${msg.role === "user" ? "user" : "bot"}`);
  node.dataset.index = index;
  if (msg.role === "user") {
    const wrap = el("div");
    wrap.append(el("div", "bubble", msg.content));
    if (msg.tags) wrap.append(el("div", "tags", msg.tags));
    node.append(wrap);
  } else {
    node.innerHTML = botHtml(msg);
  }
  return node;
}

function welcome() {
  const box = el("div", "welcome");
  box.append(el("h1", null, "Ρωτήστε πάνω στην ελληνική νομοθεσία"));
  box.append(el("p", null, "Η απάντηση συντάσσεται μόνο από τα άρθρα που ανασύρονται από το ευρετήριο και κάθε ισχυρισμός παραπέμπει στο άρθρο του, που ανοίγει δίπλα με ένα κλικ."));
  const grid = el("div", "examples");
  for (const [topic, question] of EXAMPLES) {
    const b = el("button", "example");
    b.type = "button"; b.dataset.example = question;
    b.append(el("small", null, topic), document.createTextNode(question));
    grid.append(b);
  }
  box.append(grid);
  const idx = S.meta?.index;
  if (idx) {
    const strip = el("div", "coverage-strip");
    const fmt = (n) => Number(n).toLocaleString("el-GR");
    strip.innerHTML = `<span>Σύνταγμα</span><span><b>${fmt(idx.by_kind.code)}</b> κώδικες</span><span><b>${fmt(idx.by_kind.law)}</b> νόμοι</span><span><b>${fmt(idx.by_kind.decree)}</b> π.δ.</span><span><b>${fmt(idx.units)}</b> άρθρα</span>`;
    box.append(strip);
  }
  return box;
}

function renderChats() {
  const nav = $("#chats");
  nav.replaceChildren();
  for (const chat of S.chats) {
    const row = el("div", "chat-item" + (chat === S.current ? " is-active" : ""));
    const open = el("button", "open", chat.title);
    open.type = "button"; open.title = chat.title;
    open.onclick = () => { S.current = chat; renderChats(); renderThread(); closeSidebar(); };
    const del = el("button", "del", "✕");
    del.type = "button"; del.setAttribute("aria-label", "Διαγραφή συζήτησης");
    del.onclick = () => {
      S.chats = S.chats.filter((c) => c !== chat);
      if (S.current === chat) S.current = null;
      save(); renderChats(); renderThread();
    };
    row.append(open, del);
    nav.append(row);
  }
}

/* ------------------------------------------------------------ αποστολή */

function optionTags() {
  const tags = [];
  if (S.kinds.size) tags.push([...S.kinds].map((k) => S.meta.kinds.find((x) => x.id === k)?.label).join(", "));
  if (S.acts.size) tags.push([...S.acts].map((id) => S.meta.catalogue.find((a) => a.id === id)?.short).join(", "));
  const asOf = $("#as-of").value;
  if (asOf && S.mode === "ask") tags.push(`ισχύς στις ${asOf}`);
  if (S.mode === "search") tags.push("μόνο αναζήτηση");
  return tags.join(" · ");
}

function historyFor(chat) {
  return chat.messages
    .filter((m) => m.role === "user" ? !m.searchOnly : (m.kind !== "search" && m.content && !m.error))
    .map((m) => ({ role: m.role === "user" ? "user" : "assistant", content: m.role === "user" ? m.content : plainAnswer(m) }))
    .slice(-12);
}

async function send(question) {
  question = question.trim();
  if (!question || S.busy) return;
  if (!S.current) {
    S.current = { id: Date.now().toString(36), title: question.slice(0, 70), messages: [] };
    S.chats.unshift(S.current);
  }
  const chat = S.current;
  const history = historyFor(chat);
  const lastBot = [...chat.messages].reverse().find((m) => m.role === "assistant" && m.sources?.length);
  const searchOnly = S.mode === "search";
  chat.messages.push({ role: "user", content: question, tags: optionTags(), searchOnly });
  const msg = { role: "assistant", kind: searchOnly ? "search" : "answer", content: "", pending: true,
    status: searchOnly ? "Αναζήτηση διατάξεων" : "Ανάλυση της ερώτησης", sources: [], warnings: [] };
  chat.messages.push(msg);
  renderChats(); renderThread();
  const node = $("#thread").lastElementChild;
  let queued = false;
  const paint = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const thread = $("#thread");
      const pinned = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
      node.innerHTML = botHtml(msg);
      if (pinned) thread.scrollTop = thread.scrollHeight;
    });
  };
  setBusy(true);
  try {
    if (searchOnly) {
      const params = new URLSearchParams({ q: question, kinds: [...S.kinds].join(","), acts: [...S.acts].join(","), limit: "25" });
      msg.hits = (await apiJson(`/api/search?${params}`)).hits;
    } else {
      await streamAnswer(msg, paint, {
        question, history, kinds: [...S.kinds], acts: [...S.acts], as_of: $("#as-of").value || "",
        previous_sources: (lastBot?.sources || []).map((s) => s.uid),
      });
    }
  } catch (err) {
    if (err.name !== "AbortError") msg.error = err.message || "Η σύνδεση διακόπηκε.";
  }
  msg.pending = false;
  setBusy(false);
  node.innerHTML = botHtml(msg);
  save();
}

async function streamAnswer(msg, paint, body) {
  S.abort = new AbortController();
  const resp = await api("/api/ask", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: S.abort.signal,
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.error || `Σφάλμα ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of block.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === "status") msg.status = ev.message;
        else if (ev.type === "plan") msg.plan = { searches: ev.searches, citations: ev.citations };
        else if (ev.type === "sources") { msg.sources = ev.sources; msg.warnings = ev.warnings || []; }
        else if (ev.type === "text") msg.content += ev.delta;
        else if (ev.type === "check") msg.check = ev;
        else if (ev.type === "error") msg.error = ev.message;
        paint();
      }
    }
  }
}

function setBusy(busy) {
  S.busy = busy;
  const button = $("#send");
  button.textContent = busy ? "Διακοπή" : (S.mode === "search" ? "Αναζήτηση" : "Αποστολή");
  button.type = busy ? "button" : "submit";
}

/* ------------------------------------------------------------ πίνακας διάταξης */

async function openUnit(uid) {
  const panel = $("#panel"), body = $("#panel-body");
  panel.hidden = false;
  $("#app").classList.add("has-panel");
  $("#panel-title").textContent = "Φόρτωση…";
  body.replaceChildren();
  let unit;
  try { unit = await apiJson(`/api/unit?uid=${encodeURIComponent(uid)}`); }
  catch (err) { $("#panel-title").textContent = "Η διάταξη δεν βρέθηκε"; body.append(el("p", "error", err.message)); return; }
  $("#panel-title").textContent = unit.cite;
  const act = unit.act;
  const head = el("div");
  head.innerHTML = `<div class="act-title">${esc(act.kind === "code" || act.kind === "constitution" ? act.title : `${act.short} — ${act.title}`)}${badges(unit)}</div>
    <div class="notice ${act.consolidation === "enacted" || unit.ocr ? "notice-warn" : "notice-ok"}">${esc(act.note || "")}</div>
    ${unit.context ? `<div class="context">${esc(unit.context)}</div>` : ""}
    <div class="heading">${esc(unit.label)}${unit.heading ? " · " + esc(unit.heading) : ""}</div>`;
  body.append(head, el("div", "law-text", unit.text));

  const nav = el("div", "panel-nav");
  for (const [key, label] of [["prev", "← "], ["next", ""]]) {
    if (!unit[key]) continue;
    const b = el("button", "btn btn-small", key === "prev" ? label + unit.prev.label : unit.next.label + " →");
    b.type = "button"; b.onclick = () => openUnit(unit[key].uid);
    nav.append(b);
  }
  body.append(nav);

  const links = el("div", "panel-links");
  if (act.official_url) {
    const a = el("a", "btn btn-small", "Επίσημο κείμενο (ΦΕΚ)");
    a.href = act.official_url; a.target = "_blank"; a.rel = "noopener noreferrer";
    links.append(a);
  }
  const liveBox = el("div", "live");
  if (unit.live_available) {
    const b = el("button", "btn btn-small", "Έλεγχος ισχύος και ιστορικού");
    b.type = "button";
    b.title = "Ρωτά το αρχείο Global law της Lexifina για την κατάσταση της διάταξης σήμερα";
    b.onclick = async () => {
      b.disabled = true; liveBox.textContent = "Έλεγχος στο αρχείο…";
      try { renderLive(liveBox, await apiJson(`/api/live?uid=${encodeURIComponent(unit.uid)}`)); }
      catch (err) { liveBox.innerHTML = `<div class="notice notice-warn">${esc(err.message)}</div>`; b.disabled = false; }
    };
    links.append(b);
  }
  if (links.children.length) body.append(links, liveBox);
  body.scrollTop = 0;
}

function renderLive(box, live) {
  const change = live.last_change || {};
  const history = (live.history || []).map((h) => `${esc(h.in_force || "")} — ${esc(h.law || "")}`).join("<br>");
  box.innerHTML = `<div class="notice ${live.in_force ? "notice-ok" : "notice-bad"}">${live.in_force ? "Η διάταξη είναι σε ισχύ στο αρχείο σήμερα." : "Η διάταξη δεν εμφανίζεται σε ισχύ στο αρχείο σήμερα."}</div>
    <dl>
      <dt>Τελευταία αλλαγή</dt><dd>${esc(change.law || "—")}${change.in_force ? ` (σε ισχύ από ${esc(change.in_force)})` : ""}</dd>
      <dt>Αλλαγές στο αρχείο</dt><dd>${esc(live.changes_count ?? "—")}</dd>
      ${history ? `<dt>Ιστορικό</dt><dd>${history}</dd>` : ""}
      ${live.latest ? `<dt>Κατάσταση</dt><dd>${esc(live.latest)}</dd>` : ""}
      ${live.official_url ? `<dt>Πηγή</dt><dd><a href="${esc(live.official_url)}" target="_blank" rel="noopener noreferrer">επίσημο κείμενο</a></dd>` : ""}
    </dl>
    <p class="muted">Πηγή ελέγχου: αρχείο Global law (Lexifina). Το αρχείο δεν κρατά όλες τις τροποποιήσεις κάθε πράξης.</p>`;
}

function closePanel() { $("#panel").hidden = true; $("#app").classList.remove("has-panel"); }
function closeSidebar() { $("#app").classList.remove("show-sidebar"); $("#scrim").hidden = true; }

/* ------------------------------------------------------------ επιλογές */

function buildOptions() {
  const chips = $("#kind-chips");
  for (const kind of S.meta.kinds) {
    const b = el("button", "chip", kind.label);
    b.type = "button"; b.setAttribute("aria-pressed", "false");
    b.onclick = () => {
      S.kinds.has(kind.id) ? S.kinds.delete(kind.id) : S.kinds.add(kind.id);
      b.classList.toggle("is-on", S.kinds.has(kind.id));
      b.setAttribute("aria-pressed", String(S.kinds.has(kind.id)));
    };
    chips.append(b);
  }
  const list = $("#acts-list");
  for (const act of S.meta.catalogue) {
    const label = el("label");
    const box = el("input"); box.type = "checkbox"; box.value = act.id;
    box.onchange = () => { box.checked ? S.acts.add(act.id) : S.acts.delete(act.id); actsLabel(); };
    label.append(box, el("span", "short", act.short), el("span", null, act.title));
    list.append(label);
  }
  $("#acts-button").onclick = () => $("#acts-dialog").showModal();
  $("#acts-clear").onclick = () => {
    S.acts.clear();
    list.querySelectorAll("input").forEach((i) => { i.checked = false; });
    actsLabel();
  };
  document.querySelectorAll(".seg-btn").forEach((b) => {
    b.onclick = () => {
      S.mode = b.dataset.mode;
      document.querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("is-on", x === b));
      $(".date-field").hidden = S.mode === "search";
      setBusy(S.busy);
    };
  });
  const today = new Date().toISOString().slice(0, 10);
  $("#as-of").max = today;
  if (!S.meta.live_check) $(".date-field").hidden = true;
}

function actsLabel() {
  const button = $("#acts-button");
  const names = [...S.acts].map((id) => S.meta.catalogue.find((a) => a.id === id)?.short);
  button.textContent = names.length ? `Κώδικες: ${names.slice(0, 3).join(", ")}${names.length > 3 ? ` +${names.length - 3}` : ""}` : "Κώδικες: όλοι";
  button.classList.toggle("is-on", names.length > 0);
}

function showCoverage() {
  const idx = S.meta.index;
  const fmt = (n) => Number(n).toLocaleString("el-GR");
  const state = { enacted: "όπως εκδόθηκε", amended: "με τροποποιήσεις", consolidated: "κωδικοποίηση" };
  const rows = S.meta.catalogue.map((a) => `<tr><td><b>${esc(a.short)}</b></td><td>${esc(a.title)}</td><td>${state[a.consolidation]}${a.ocr ? " · OCR" : ""}</td><td>${fmt(a.n_units)}</td></tr>`).join("");
  $("#coverage-body").innerHTML = `
    <p>Το ευρετήριο περιέχει το Σύνταγμα, ${fmt(idx.by_kind.code)} κώδικες, ${fmt(idx.by_kind.law)} νόμους και ${fmt(idx.by_kind.decree)} προεδρικά διατάγματα (${fmt(idx.units)} άρθρα). Δεν είναι πλήρες και δεν περιέχει νομολογία, υπουργικές αποφάσεις ή εγκυκλίους.</p>
    <div class="notice notice-warn">Οι περισσότερες πράξεις, μεταξύ των οποίων ο Αστικός Κώδικας και ο Κώδικας Πολιτικής Δικονομίας, υπάρχουν <b>όπως αρχικά δημοσιεύθηκαν στο ΦΕΚ</b>, χωρίς τις μεταγενέστερες τροποποιήσεις· ορισμένες προέρχονται από OCR σαρωμένων φύλλων. Κάθε πηγή φέρει τη σχετική ένδειξη. Ελέγχετε πάντα το ισχύον κείμενο.</div>
    <div class="cov-wrap"><table class="cov-table"><thead><tr><th>Συντ.</th><th>Τίτλος</th><th>Κείμενο</th><th>Άρθρα</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="muted">Πηγή κειμένων: <a href="${esc(idx.source)}" target="_blank" rel="noopener noreferrer">lexifina/greece-law</a>${idx.source_commit ? ` (έκδοση ${esc(idx.source_commit)})` : ""}, ευρετήριο της ${esc((idx.built_at || "").slice(0, 10))}. Τα επίσημα κείμενα δημοσιεύονται από το Εθνικό Τυπογραφείο.</p>`;
  $("#coverage-dialog").showModal();
}

/* ------------------------------------------------------------ εκκίνηση */

function autosize() {
  const area = $("#question");
  area.style.height = "auto";
  area.style.height = Math.min(area.scrollHeight, 220) + "px";
}

async function init() {
  load();
  try { S.meta = await (await fetch("/api/meta")).json(); }
  catch { $("#thread").append(el("p", "error", "Η υπηρεσία δεν απαντά. Δοκιμάστε να ανανεώσετε τη σελίδα.")); return; }
  if (S.meta.auth_required && !S.code) await askCode();
  buildOptions();
  const pill = $("#mode-pill");
  if (S.meta.llm === "fake") { pill.textContent = "Λειτουργία επίδειξης: χωρίς μοντέλο"; pill.hidden = false; }
  if (S.meta.llm === "off") { pill.textContent = "Χωρίς μοντέλο: μόνο αναζήτηση"; pill.hidden = false; $('.seg-btn[data-mode="search"]').click(); }
  renderChats(); renderThread();

  $("#composer").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const area = $("#question");
    const question = area.value;
    if (!question.trim() || S.busy) return;
    area.value = ""; autosize();
    send(question);
  });
  $("#send").addEventListener("click", () => { if (S.busy && S.abort) S.abort.abort(); });
  $("#question").addEventListener("input", autosize);
  $("#question").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#new-chat").onclick = () => { S.current = null; renderChats(); renderThread(); closeSidebar(); $("#question").focus(); };
  $("#open-coverage").onclick = showCoverage;
  $("#close-panel").onclick = closePanel;
  $("#toggle-sidebar").onclick = () => { $("#app").classList.add("show-sidebar"); $("#scrim").hidden = false; };
  $("#scrim").onclick = closeSidebar;
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("#panel").hidden && !document.querySelector("dialog[open]")) closePanel(); });
  $("#thread").addEventListener("click", (ev) => {
    const target = ev.target.closest("[data-uid], [data-example], [data-copy]");
    if (!target) return;
    if (target.dataset.uid) openUnit(target.dataset.uid);
    else if (target.dataset.example) send(target.dataset.example);
    else if ("copy" in target.dataset) {
      const index = +target.closest(".msg").dataset.index;
      const msg = S.current.messages[index];
      const sources = (msg.sources || []).map((s) => `[${s.n}] ${s.cite}${s.heading ? " — " + s.heading : ""}`).join("\n");
      navigator.clipboard?.writeText(plainAnswer(msg) + (sources ? "\n\nΠηγές:\n" + sources : ""))
        .then(() => { target.textContent = "Αντιγράφηκε"; setTimeout(() => { target.textContent = "Αντιγραφή απάντησης"; }, 1600); })
        .catch(() => { target.textContent = "Η αντιγραφή δεν επιτράπηκε"; });
    }
  });
}

init();
