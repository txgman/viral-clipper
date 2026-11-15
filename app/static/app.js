"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (s) => {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(sec).padStart(2, "0")}`;
};

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

// ------------------------------------------------------------------ status + settings
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const pill = $("#ai-pill");
    pill.className = "pill " + (s.ai ? "ok" : "warn");
    pill.textContent = s.ai ? "AI: Claude ✓" : "AI: basic · add key";
    pill.title = s.ai ? "Claude picks the moments" : "Add an Anthropic API key in Settings for smarter picks";
    pill.onclick = s.ai ? null : openSettings;
    const banners = [];
    if (!s.ffmpeg) banners.push(["bad", "FFmpeg is not installed, so videos can't be processed. Install FFmpeg and restart the app (see README)."]);
    if (!s.js_runtime) banners.push(["warn", "YouTube downloads need Deno or Node.js installed on this computer. Other sites and uploads work without it."]);
    $("#banners").innerHTML = banners.map(([k, m]) => `<div class="banner ${k}">${esc(m)}</div>`).join("");
  } catch (_) { /* server restarting */ }
}

async function openSettings() {
  const s = await api("/api/settings");
  const f = $("#settings-form");
  f.anthropic_api_key.value = "";
  f.anthropic_api_key.placeholder = s.anthropic_api_key_set ? `Saved (${s.anthropic_api_key_hint || "set"}). Type to replace` : "sk-ant-…";
  const known = [...f.whisper_model.options].map((o) => o.value);
  if (!known.includes(s.whisper_model)) f.whisper_model.add(new Option(s.whisper_model, s.whisper_model));
  f.whisper_model.value = s.whisper_model;
  f.cookies_from_browser.value = s.cookies_from_browser || "";
  $("#clear-key").classList.toggle("hidden", !s.anthropic_api_key_set);
  $("#settings").showModal();
}

$("#open-settings").onclick = openSettings;
$("#clear-key").onclick = async () => {
  await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ anthropic_api_key: "" }) });
  $("#settings").close();
  toast("API key removed");
  refreshStatus();
};
$("#save-settings").onclick = async (e) => {
  e.preventDefault();
  const f = $("#settings-form");
  const body = { whisper_model: f.whisper_model.value, cookies_from_browser: f.cookies_from_browser.value };
  if (f.anthropic_api_key.value.trim()) body.anthropic_api_key = f.anthropic_api_key.value.trim();
  try {
    await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#settings").close();
    toast("Settings saved");
    refreshStatus();
  } catch (err) { toast(err.message); }
};

// ------------------------------------------------------------------ form
let mode = "link";
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  mode = tab.dataset.tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== mode));
}));

const drop = $("#drop"), fileInput = $("#file");
const showFile = () => {
  const f = fileInput.files[0];
  $("#drop-text").innerHTML = f ? `<b>${esc(f.name)}</b><br>${(f.size / 1e6).toFixed(1)} MB · click to change`
    : "<b>Drop a video here</b> or click to choose";
};
fileInput.addEventListener("change", showFile);
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; showFile(); }
});

$("#job-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target, msg = $("#form-msg"), btn = $("#submit");
  msg.textContent = "";
  const fd = new FormData();
  if (mode === "link") {
    const url = form.url.value.trim();
    if (!url) { msg.textContent = "Paste a video link first."; return; }
    fd.append("url", url);
  } else {
    if (!fileInput.files[0]) { msg.textContent = "Choose a video file first."; return; }
    fd.append("file", fileInput.files[0]);
  }
  const [lo, hi] = form.length.value.split("-");
  fd.append("count", form.count.value);
  fd.append("min_len", lo);
  fd.append("max_len", hi);
  fd.append("layout", form.layout.value);
  fd.append("captions", form.captions.value);
  fd.append("language", form.language.value);

  btn.disabled = true;
  const xhr = new XMLHttpRequest();  // XHR so we can show upload progress
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (ev) => {
    if (mode === "file" && ev.lengthComputable) btn.textContent = `Uploading… ${Math.round((ev.loaded / ev.total) * 100)}%`;
  };
  xhr.onload = () => {
    btn.disabled = false;
    btn.textContent = "✨ Find viral clips";
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status >= 200 && xhr.status < 300) {
      form.url.value = "";
      fileInput.value = "";
      showFile();
      toast("Started! Your clips are being made.");
      poll(true);
    } else {
      msg.textContent = data.detail || `Something went wrong (${xhr.status}).`;
    }
  };
  xhr.onerror = () => { btn.disabled = false; btn.textContent = "✨ Find viral clips"; msg.textContent = "Could not reach the app server."; };
  xhr.send(fd);
});

// ------------------------------------------------------------------ jobs rendering
const jobEls = new Map();   // job id -> element
const clipKeys = new Map(); // job id -> signature of rendered clips

function jobCard(job) {
  const el = document.createElement("article");
  el.className = "job card";
  el.innerHTML = `
    <div class="job-head">
      <div>
        <h3 class="job-title"></h3>
        <div class="job-meta"></div>
      </div>
      <div class="job-actions"></div>
    </div>
    <div class="progress-wrap"><div class="progress"><div></div></div><div class="stage"><span></span><span></span></div></div>
    <div class="note hidden"></div>
    <div class="error hidden"></div>
    <div class="clips"></div>`;
  el.addEventListener("click", onJobClick);
  return el;
}

function updateJob(el, job) {
  const active = job.status === "queued" || job.status === "running";
  $(".job-title", el).innerHTML = `${esc(job.title)}<span class="status ${job.status}">${esc(job.status === "done" ? "ready" : job.status)}</span>`;
  const src = job.source_type === "url" ? `<a href="${esc(job.source)}" target="_blank" rel="noopener">${esc(job.source)}</a>` : `Uploaded file: ${esc(job.source)}`;
  $(".job-meta", el).innerHTML = `${src}${job.duration ? ` · ${fmt(job.duration)} long` : ""}`;

  const done = job.clips.filter((c) => c.status === "done").length;
  $(".job-actions", el).innerHTML =
    (done ? `<a class="btn small" href="/api/jobs/${job.id}/zip">⬇ Download all (${done})</a>` : "") +
    (active ? `<button class="btn small danger" data-act="cancel">Cancel</button>`
            : `<button class="btn small danger" data-act="delete">Delete</button>`);

  $(".progress-wrap", el).classList.toggle("hidden", !active);
  if (active) {
    $(".progress > div", el).style.width = `${job.progress || 0}%`;
    const [a, b] = el.querySelectorAll(".stage span");
    a.textContent = job.stage || "";
    b.textContent = `${Math.round(job.progress || 0)}%`;
  }
  const note = $(".note", el);
  const aiText = job.ai_mode === "claude" ? "Moments picked by Claude." : job.ai_note;
  note.textContent = aiText || "";
  note.className = "note" + (job.ai_mode === "claude" ? " ai" : "") + (aiText ? "" : " hidden");
  const err = $(".error", el);
  err.textContent = job.error || "";
  err.classList.toggle("hidden", !job.error);

  const key = job.clips.map((c) => `${c.index}:${c.status}`).join(",");
  if (clipKeys.get(job.id) !== key) {
    clipKeys.set(job.id, key);
    renderClips($(".clips", el), job);
  }
}

function renderClips(container, job) {
  const existing = new Map([...container.children].map((c) => [c.dataset.key, c]));
  const nodes = job.clips.map((c) => {
    const key = `${c.index}:${c.status}`;
    return existing.get(key) || clipCard(job, c, key);
  });
  container.replaceChildren(...nodes);
}

function clipCard(job, c, key) {
  const el = document.createElement("div");
  el.className = "clip";
  el.dataset.key = key;
  const base = `/api/jobs/${job.id}/clips/${c.index}`;
  const scoreCls = c.score >= 85 ? "hot" : c.score >= 70 ? "warm" : "ok";
  const ready = c.status === "done";
  const tags = (c.hashtags || []).join(" ");
  el.innerHTML = `
    <div class="player">
      ${ready ? `<video src="${base}/video" poster="${base}/thumb" controls playsinline preload="none"></video>`
              : `<div class="wait">${c.status === "rendering" ? "🎬 Rendering…" : "⏳ Waiting to render"}</div>`}
      <span class="score ${scoreCls}">🔥 ${esc(c.score)}</span>
    </div>
    <div class="clip-body">
      <div class="clip-title">${esc(c.title)}</div>
      <div class="clip-time">${fmt(c.start)} – ${fmt(c.end)} · ${Math.round(c.duration)} s${c.layout ? ` · ${({ track: "face tracking", fit: "full frame", center: "center crop", mixed: "face tracking + full frame" })[c.layout] || c.layout}` : ""}</div>
      ${c.reason ? `<div class="clip-reason">${esc(c.reason)}</div>` : ""}
      ${(c.caption || tags) ? `<div class="post">${esc(c.caption)}${c.caption && tags ? "\n" : ""}<span class="tags">${esc(tags)}</span></div>` : ""}
      ${c.transcript ? `<details class="transcript"><summary>Transcript</summary><p>${esc(c.transcript)}</p></details>` : ""}
      <div class="clip-actions">
        ${ready ? `<a class="btn small primary" href="${base}/video?download=true">⬇ Download</a>` : ""}
        ${(c.caption || tags) ? `<button class="btn small" data-copy="${esc([c.title, c.caption, tags].filter(Boolean).join("\n\n"))}">Copy caption</button>` : ""}
      </div>
    </div>`;
  return el;
}

async function onJobClick(e) {
  const btn = e.target.closest("button");
  if (!btn) return;
  const el = e.currentTarget, id = el.dataset.id;
  if (btn.dataset.copy !== undefined) {
    try { await navigator.clipboard.writeText(btn.dataset.copy); toast("Caption copied"); }
    catch (_) { toast("Copy failed. Select the text manually."); }
    return;
  }
  if (btn.dataset.act === "cancel") {
    await api(`/api/jobs/${id}/cancel`, { method: "POST" });
    toast("Cancelling…");
  } else if (btn.dataset.act === "delete") {
    if (!confirm("Delete this video and its clips?")) return;
    await api(`/api/jobs/${id}`, { method: "DELETE" });
    el.remove();
    jobEls.delete(id);
    clipKeys.delete(id);
  }
  poll(true);
}

let pollTimer = null;
async function poll(immediate = false) {
  clearTimeout(pollTimer);
  let jobs = [];
  try {
    jobs = await api("/api/jobs");
    const list = $("#jobs");
    const seen = new Set();
    jobs.forEach((job, i) => {
      seen.add(job.id);
      let el = jobEls.get(job.id);
      if (!el) {
        el = jobCard(job);
        el.dataset.id = job.id;
        jobEls.set(job.id, el);
      }
      if (list.children[i] !== el) list.insertBefore(el, list.children[i] || null);
      updateJob(el, job);
    });
    for (const [id, el] of jobEls) if (!seen.has(id)) { el.remove(); jobEls.delete(id); clipKeys.delete(id); }
    $("#empty").classList.toggle("hidden", jobs.length > 0);
  } catch (_) { /* retry below */ }
  const active = jobs.some((j) => j.status === "queued" || j.status === "running");
  pollTimer = setTimeout(poll, active ? 1500 : 8000);
}

refreshStatus();
poll(true);
