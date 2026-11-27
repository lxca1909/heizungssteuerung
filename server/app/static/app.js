"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const COLORS = ["#ea580c", "#2563eb", "#16a34a", "#9333ea"];
const STALE_S = 90;

const state = { heaters: {}, agentOnline: null, alarms: [], es: null, cards: {} };

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* leer */ }
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Nicht angemeldet"); }
  if (!res.ok) throw new Error((data && (data.detail || data.message)) || `Fehler ${res.status}`);
  return data;
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 5000);
}

// --------------------------------------------------------------- Login
function showLogin() {
  if (state.es) { state.es.close(); state.es = null; }
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const err = $("#login-error");
  err.hidden = true;
  try {
    await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    e.target.reset();
    startApp();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ----------------------------------------------------------------- App
async function startApp() {
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  connectEvents();
  loadHistory();
}

function connectEvents() {
  if (state.es) state.es.close();
  const es = new EventSource("/api/events");
  state.es = es;
  es.addEventListener("snapshot", (e) => {
    const s = JSON.parse(e.data);
    state.agentOnline = s.agent_online;
    state.heaters = {};
    for (const h of s.heaters) state.heaters[h.id] = h;
    state.alarms = s.alarms;
    renderAll();
  });
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "heater") { state.heaters[ev.data.id] = ev.data; renderHeater(ev.data); }
    else if (ev.type === "agent") { state.agentOnline = ev.online; renderAgent(); renderAll(); }
    else if (ev.type === "alarm") { state.alarms.unshift(ev.data); renderEvents(); renderBanner(); }
  };
  es.onerror = async () => {
    // Bei abgelaufener Sitzung liefert /api/me 401 -> Login.
    es.close();
    try { await api("/api/me"); setTimeout(connectEvents, 3000); } catch { /* Login angezeigt */ }
  };
}

function renderAll() {
  renderAgent();
  renderBanner();
  renderEvents();
  for (const h of Object.values(state.heaters)) renderHeater(h);
}

function renderAgent() {
  const p = $("#agent-pill");
  if (state.agentOnline === true) { p.textContent = "Werkstatt online"; p.className = "pill ok"; }
  else if (state.agentOnline === false) { p.textContent = "Werkstatt offline"; p.className = "pill bad"; }
  else { p.textContent = "unbekannt"; p.className = "pill"; }
}

function renderBanner() {
  const b = $("#banner");
  const now = Date.now() / 1000;
  const recent = state.alarms.find((a) => a.level === "critical" && now - a.ts < 3600);
  if (recent) { b.textContent = "⚠ " + recent.message; b.hidden = false; }
  else if (state.agentOnline === false) { b.textContent = "Werkstatt-PC ist offline – keine Steuerung möglich."; b.hidden = false; }
  else b.hidden = true;
}

const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

function renderEvents() {
  const ul = $("#events");
  ul.replaceChildren();
  if (!state.alarms.length) { ul.innerHTML = '<li class="muted">Keine Ereignisse</li>'; return; }
  for (const a of state.alarms.slice(0, 15)) {
    const li = document.createElement("li");
    const t = document.createElement("time");
    t.textContent = fmtTime(a.ts);
    const span = document.createElement("span");
    span.className = a.level || "";
    span.textContent = (a.name ? a.name + ": " : "") + a.message;
    li.append(t, span);
    ul.append(li);
  }
}

// -------------------------------------------------------------- Heizung
function card(id) {
  if (state.cards[id]) return state.cards[id];
  const el = $("#heater-tpl").content.firstElementChild.cloneNode(true);
  const minutes = () => Number($(".h-minutes", el).value);
  $(".h-vollgas", el).onclick = () => command(id, "start", { mode: "vollgas", minutes: minutes() }, el);
  $(".h-tempstart", el).onclick = () => {
    const temp = Number($(".h-temp", el).value);
    if (!(temp >= 8 && temp <= 36)) return toast("Temperatur zwischen 8 und 36 °C", true);
    command(id, "start", { mode: "temperatur", temp, minutes: minutes() }, el);
  };
  $(".h-fan", el).onclick = () =>
    command(id, "start", { mode: "lueften", level: Number($(".h-fanlevel", el).value), minutes: minutes() }, el);
  el.querySelectorAll(".h-extend").forEach((b) => {
    b.onclick = () => command(id, "extend", { minutes: Number(b.dataset.min) }, el);
  });
  $(".h-stop", el).onclick = () => {
    if (confirm("Heizung ausschalten?")) command(id, "stop", null, el);
  };
  el.dataset.id = id;
  const list = $("#heaters");
  list.append(el);
  [...list.children].sort((a, b) => a.dataset.id.localeCompare(b.dataset.id)).forEach((c) => list.append(c));
  state.cards[id] = el;
  return el;
}

function renderHeater(h) {
  const el = card(h.id);
  const s = h.status;
  const now = Date.now() / 1000;
  const stale = !h.ts || now - h.ts > STALE_S || state.agentOnline === false;
  $(".h-name", el).textContent = h.name || "Heizung " + h.id;

  const badge = $(".h-badge", el);
  let label = "Aus", cls = "pill";
  if (!h.connected || !s) { label = "Nicht verbunden"; cls = "pill bad"; }
  else if (s.error_code) { label = "Fehler " + s.error_code; cls = "pill bad"; }
  else if (s.on) {
    label = s.mode === "lueften" ? "Lüftet" : s.phase === "zuendung" ? "Zündet" : s.phase === "selbsttest" ? "Startet" : "Heizt";
    cls = "pill heat";
  }
  else if (s.cooling_down) { label = "Kühlt ab"; cls = "pill warn"; }
  if (stale) { label += " (veraltet)"; }
  badge.textContent = label;
  badge.className = cls + " h-badge";

  const fmt = (v, unit, digits = 0) => (v === null || v === undefined ? "–" : Number(v).toFixed(digits) + unit);
  $(".h-room", el).textContent = s ? fmt(s.room_temp, " °C") : "–";
  $(".h-mode", el).textContent = !s ? "–"
    : s.mode === "vollgas" ? "Vollgas"
    : s.mode === "temperatur" ? `${s.set_temp} °C`
    : s.mode === "lueften" ? "Lüften" : s.mode;
  $(".h-gear", el).textContent = s && s.on && s.gear != null ? `${s.gear} / 10` : "–";
  $(".h-case", el).textContent = s ? fmt(s.case_temp, " °C") : "–";
  $(".h-voltage", el).textContent = s ? fmt(s.voltage, " V", 1) : "–";
  $(".h-remaining", el).textContent = s && s.on && h.remaining_min != null ? fmtMinutes(h.remaining_min) : "–";

  let note = "";
  if (h.last_error) note = "Verbindung: " + h.last_error;
  else if (s && s.on && h.off_at) note = "Schaltet automatisch aus um " + new Date(h.off_at * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  else if (h.last_seen) note = "Zuletzt aktualisiert " + new Date(h.last_seen * 1000).toLocaleTimeString("de-DE");
  $(".h-note", el).textContent = note;

  const canStart = h.connected && !!s && state.agentOnline !== false && !s.cooling_down;
  $(".h-vollgas", el).disabled = !canStart || el.dataset.busy === "1";
  $(".h-tempstart", el).disabled = !canStart || el.dataset.busy === "1";
  const running = !!(h.connected && s && s.on);
  $(".h-stop", el).disabled = !running || el.dataset.busy === "1";
  $(".extend-row", el).hidden = !running;
  $(".fan-row", el).hidden = !running;
  $(".h-fan", el).disabled = el.dataset.busy === "1" || state.agentOnline === false;
  el.querySelectorAll(".h-extend").forEach((b) => (b.disabled = el.dataset.busy === "1" || state.agentOnline === false));
}

function fmtMinutes(m) {
  m = Math.round(m);
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m} min`;
}

async function command(id, action, body, el) {
  el.dataset.busy = "1";
  el.querySelectorAll("button").forEach((b) => (b.disabled = true));
  toast({ stop: "Schalte aus …", extend: "Verlängere …" }[action] || "Starte …");
  try {
    const r = await api(`/api/heater/${encodeURIComponent(id)}/${action}`, { method: "POST", body: body || undefined });
    toast(r.message || (r.ok ? "OK" : "Fehlgeschlagen"), !r.ok);
  } catch (ex) {
    toast(ex.message, true);
  } finally {
    el.dataset.busy = "";
    renderHeater(state.heaters[id]);
  }
}

// ---------------------------------------------------------------- Chart
async function loadHistory() {
  const hours = Number($("#range").value);
  try {
    const data = await api(`/api/history?hours=${hours}`);
    drawChart(data.series, hours);
  } catch { /* egal */ }
}
$("#range").addEventListener("change", loadHistory);
setInterval(() => { if (!$("#app-view").hidden) loadHistory(); }, 60_000);

function drawChart(series, hours) {
  const box = $("#chart");
  const W = box.clientWidth || 600, H = box.clientHeight || 220, L = 34, R = 8, T = 8, B = 22;
  const now = Date.now() / 1000, t0 = now - hours * 3600;
  const pts = Object.values(series).flat().filter((p) => p[1] != null);
  if (!pts.length) { box.innerHTML = '<p class="muted">Noch keine Messwerte.</p>'; $("#legend").replaceChildren(); return; }
  let lo = Math.min(...pts.map((p) => p[1])), hi = Math.max(...pts.map((p) => p[1]));
  lo = Math.floor(lo / 5) * 5; hi = Math.ceil((hi + 0.1) / 5) * 5;
  const x = (t) => L + ((t - t0) / (now - t0)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Raumtemperatur-Verlauf">`;
  for (let v = lo; v <= hi; v += 5) {
    svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`;
    svg += `<text class="axis" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v}°</text>`;
  }
  const ticks = hours <= 24 ? 6 : 7;
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + (i / ticks) * (now - t0);
    const d = new Date(t * 1000);
    const lbl = hours <= 24 ? d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString("de-DE", { weekday: "short" });
    const anchor = i === 0 ? "start" : i === ticks ? "end" : "middle";
    svg += `<text class="axis" x="${x(t)}" y="${H - 6}" text-anchor="${anchor}">${lbl}</text>`;
  }
  const legend = [];
  Object.entries(series).forEach(([id, rows], i) => {
    const color = COLORS[i % COLORS.length];
    // Heizphasen als Fläche
    let start = null;
    const band = (a, b) => `<rect x="${x(a)}" y="${T}" width="${Math.max(1, x(b) - x(a))}" height="${H - T - B}" fill="${color}" opacity=".08"/>`;
    rows.forEach((r, j) => {
      if (r[2] && start === null) start = r[0];
      if ((!r[2] || j === rows.length - 1) && start !== null) { svg += band(start, r[0]); start = null; }
    });
    const d = rows.filter((r) => r[1] != null).map((r, j) => `${j ? "L" : "M"}${x(r[0]).toFixed(1)},${y(r[1]).toFixed(1)}`).join("");
    svg += `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>`;
    const name = state.heaters[id]?.name || "Heizung " + id;
    legend.push(`<span><i style="background:${color}"></i>${escapeHtml(name)}</span>`);
  });
  box.innerHTML = svg + "</svg>";
  $("#legend").innerHTML = legend.join("") + '<span class="muted">Fläche = Heizung an</span>';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Restlaufzeit/„veraltet“ regelmäßig neu berechnen
setInterval(() => Object.values(state.heaters).forEach(renderHeater), 30_000);

// ---------------------------------------------------------------- Start
api("/api/me").then(startApp).catch(() => showLogin());
