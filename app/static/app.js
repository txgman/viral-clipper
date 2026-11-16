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
const jobsById = new Map(); // job id -> latest job data
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
    (done ? `<button class="btn small" data-act="tt-all">♪ Post all to TikTok</button>` : "") +
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
  updateClipStatuses();
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
      <div class="tt-status" data-tt="${job.id}:${c.index}"></div>
      ${ready ? `<button class="btn small" data-act="tt-post" data-index="${c.index}">♪ Post to TikTok</button>` : ""}
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
  if (btn.dataset.act === "tt-post" || btn.dataset.act === "tt-all") {
    const job = jobsById.get(id);
    const clips = job.clips.filter((c) => c.status === "done" &&
      (btn.dataset.act === "tt-all" || c.index === Number(btn.dataset.index)));
    openPostDialog(job, clips);
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
      jobsById.set(job.id, job);
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


// ------------------------------------------------------------------ TikTok
const PRIVACY_LABELS = {
  PUBLIC_TO_EVERYONE: "Everyone", MUTUAL_FOLLOW_FRIENDS: "Friends",
  FOLLOWER_OF_CREATOR: "Followers", SELF_ONLY: "Only me",
};
const POST_STATUS = {
  scheduled: "Scheduled", uploading: "Uploading", processing: "Processing on TikTok", posted: "Posted",
  posted_unconfirmed: "Sent (check your profile)", in_drafts: "In your TikTok drafts", failed: "Failed", cancelled: "Cancelled",
};
const MUSIC_LINK = '<a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank" rel="noopener">Music Usage Confirmation</a>';
const BC_LINK = '<a href="https://www.tiktok.com/legal/page/global/bc-policy/en" target="_blank" rel="noopener">Branded Content Policy</a>';
let tt = { configured: false, account: null };
let ttPosts = [];
const jsonPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const when = (sec) => new Date(sec * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

async function refreshTikTok() {
  try { tt = await api("/api/tiktok/status"); } catch (_) { return; }
  const btn = $("#open-tiktok"), acc = tt.account;
  btn.classList.toggle("connected", !!acc && !acc.expired);
  $("#tt-label").textContent = acc ? (acc.display_name || "TikTok connected") : "Connect TikTok";
  if ($("#tiktok-dialog").open) renderTikTokDialog();
}

function renderTikTokDialog() {
  const body = $("#tt-body"), acc = tt.account;
  if (!tt.configured) {
    body.innerHTML = `
      <p class="hint">To post for you, TikTok requires your own (free) TikTok developer app. One-time setup, about 10 minutes:</p>
      <ol class="steps">
        <li>Go to <a href="https://developers.tiktok.com/apps" target="_blank" rel="noopener">developers.tiktok.com</a>, sign in, open <b>Manage apps</b> and click <b>Connect an app</b>.</li>
        <li>Switch the toggle next to the app's name to <b>Sandbox</b> and create a sandbox. That lets you post without waiting for TikTok's review.</li>
        <li>Add the products <b>Login Kit</b> and <b>Content Posting API</b>. In Content Posting API, turn on <b>Direct Post</b>.</li>
        <li>Make sure these scopes are on: <code>user.info.basic</code>, <code>video.publish</code>, <code>video.upload</code>.</li>
        <li>In Login Kit, choose platform <b>Desktop</b> and add this redirect URI: <code class="uri">${esc(tt.redirect_uri)}</code></li>
        <li>Under <b>Sandbox settings → Target users</b>, add your TikTok account.</li>
        <li>Copy the <b>Client key</b> and <b>Client secret</b> into the boxes below.</li>
      </ol>
      <label class="field">Client key<input type="text" id="tt-key" autocomplete="off"></label>
      <label class="field">Client secret<input type="password" id="tt-secret" autocomplete="off">
        <small>Saved only on this computer. Never share it or post it online.</small></label>
      <div><button class="btn primary" id="tt-save">Save and continue</button></div>`;
    $("#tt-save").onclick = async () => {
      const key = $("#tt-key").value.trim(), secret = $("#tt-secret").value.trim();
      if (!key || !secret) return toast("Paste both the Client key and Client secret.");
      await jsonPost("/api/settings", { tiktok_client_key: key, tiktok_client_secret: secret });
      await refreshTikTok();
    };
    return;
  }
  if (!acc || acc.expired) {
    body.innerHTML = `
      ${acc && acc.expired ? '<div class="error">Your TikTok connection expired. Connect again.</div>' : ""}
      <p class="hint">Log in to TikTok and allow Viral Clipper to post videos to your account.</p>
      <div><a class="btn primary" href="/api/tiktok/connect">♪ Connect TikTok account</a></div>
      <p class="hint">Using a sandbox app? The account you connect must be added as a target user, and set to private.</p>
      <p class="hint"><a href="#" id="tt-rekey">Change TikTok app keys</a></p>`;
    $("#tt-rekey").onclick = async (e) => {
      e.preventDefault();
      await jsonPost("/api/settings", { tiktok_client_key: "", tiktok_client_secret: "" });
      refreshTikTok();
    };
    return;
  }
  const perms = [acc.can_post ? "direct posting ✓" : "no direct posting", acc.can_draft ? "drafts ✓" : "no drafts"].join(" · ");
  body.innerHTML = `
    <div class="tt-account">
      ${acc.avatar_url ? `<img src="${esc(acc.avatar_url)}" alt="" onerror="this.style.visibility='hidden'">` : "<img alt=''>"}
      <div><div class="name">${esc(acc.display_name || "TikTok account")}</div><div class="perm">${perms}</div></div>
      <button class="btn small danger" id="tt-disconnect">Disconnect</button>
    </div>
    <h3 style="margin:4px 0 0">Posts</h3>
    <div class="posts" id="tt-posts"></div>`;
  $("#tt-disconnect").onclick = async () => {
    if (!confirm("Disconnect your TikTok account? Scheduled posts won't go out until you reconnect.")) return;
    await jsonPost("/api/tiktok/disconnect");
    refreshTikTok();
  };
  renderPostList();
}

function renderPostList() {
  const box = $("#tt-posts");
  if (!box) return;
  if (!ttPosts.length) { box.innerHTML = '<p class="hint">No posts yet. Use “Post to TikTok” on any clip.</p>'; return; }
  box.innerHTML = [...ttPosts].reverse().map((p) => `
    <div class="post-row" data-post="${p.id}">
      <span class="badge ${p.status}">${esc(POST_STATUS[p.status] || p.status)}${p.status === "uploading" ? ` ${Math.round(p.progress || 0)}%` : ""}</span>
      <div class="grow">
        <div class="t">${esc(p.clip_title || `Clip ${p.clip_index}`)}</div>
        <div class="sub">${p.mode === "draft" ? "To drafts" : esc(PRIVACY_LABELS[p.privacy_level] || "")} · ${p.status === "scheduled" ? "at " : ""}${when(p.posted_at || p.scheduled_at)}</div>
        ${p.error ? `<div class="err">${esc(p.error)}</div>` : ""}
      </div>
      ${p.status === "scheduled" ? '<button class="btn small" data-pact="cancel">Cancel</button>' : ""}
      ${["failed", "cancelled"].includes(p.status) ? '<button class="btn small" data-pact="retry">Retry</button><button class="btn small ghost" data-pact="remove">✕</button>' : ""}
    </div>`).join("");
}

$("#tt-body").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-pact]");
  if (!b) return;
  const id = b.closest("[data-post]").dataset.post, act = b.dataset.pact;
  try {
    if (act === "remove") await api(`/api/tiktok/posts/${id}`, { method: "DELETE" });
    else await jsonPost(`/api/tiktok/posts/${id}/${act}`);
  } catch (err) { toast(err.message); }
  pollPosts();
});

function updateClipStatuses() {
  const latest = new Map();
  for (const p of ttPosts) latest.set(`${p.job_id}:${p.clip_index}`, p);  // sorted by time: last wins
  document.querySelectorAll(".tt-status").forEach((el) => {
    const p = latest.get(el.dataset.tt);
    el.innerHTML = p ? `<span class="badge ${p.status}">♪ ${esc(POST_STATUS[p.status] || p.status)}</span>${p.status === "scheduled" ? esc(when(p.scheduled_at)) : ""}` : "";
    el.title = p && p.error ? p.error : "";
  });
}

let postsTimer = null;
async function pollPosts() {
  clearTimeout(postsTimer);
  try { ttPosts = await api("/api/tiktok/posts"); } catch (_) {}
  updateClipStatuses();
  renderPostList();
  const busy = ttPosts.some((p) => ["uploading", "processing"].includes(p.status) ||
    (p.status === "scheduled" && p.scheduled_at * 1000 < Date.now() + 60000));
  postsTimer = setTimeout(pollPosts, busy ? 3000 : 15000);
}

$("#open-tiktok").onclick = async () => {
  await refreshTikTok();
  renderTikTokDialog();
  $("#tiktok-dialog").showModal();
};
document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

// ---------------- post dialog
let pd = null; // { job, clips, creator }

function defaultCaption(c) {
  const tags = (c.hashtags || []).filter((t) => !/^#(shorts|reels)$/i.test(t)).join(" ");
  return [c.caption || c.title || "", tags].filter(Boolean).join("\n\n");
}

async function openPostDialog(job, clips) {
  $("#pd-creator").textContent = "Loading your TikTok account…";
  $("#pd-items").innerHTML = "";
  await refreshTikTok();
  if (!tt.account || tt.account.expired) {
    renderTikTokDialog();
    $("#tiktok-dialog").showModal();
    return toast("Connect your TikTok account first.");
  }
  pd = { job, clips, creator: null };
  const f = $("#post-form");
  f.reset();
  $("#pd-heading").textContent = clips.length > 1 ? `Post ${clips.length} clips to TikTok` : "Post to TikTok";
  $("#pd-error").classList.add("hidden");
  $("#pd-creator").textContent = "Loading your TikTok account…";
  $("#pd-interval-wrap").classList.toggle("hidden", clips.length < 2);
  const at = new Date(Date.now() + 60 * 60 * 1000);
  at.setMinutes(0, 0, 0);
  $("#pd-at").value = new Date(at.getTime() - at.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  if (!tt.account.can_post) f.querySelector('input[value="draft"]').checked = true;
  f.querySelector('input[value="direct"]').disabled = !tt.account.can_post;
  f.querySelector('input[value="draft"]').disabled = !tt.account.can_draft;

  $("#pd-items").innerHTML = clips.map((c) => `
    <div class="pd-item" data-index="${c.index}">
      <video src="/api/jobs/${job.id}/clips/${c.index}/video" poster="/api/jobs/${job.id}/clips/${c.index}/thumb" controls playsinline preload="none"></video>
      <div class="pi-body">
        <div class="pi-title">${esc(c.title)} <span class="hint">· ${Math.round(c.duration)} s</span></div>
        <textarea maxlength="2200" aria-label="Caption">${esc(defaultCaption(c))}</textarea>
        <div class="pi-row">
          <button type="button" class="btn small" data-smart>✨ Smart caption</button>
          <button type="button" class="btn small ghost" data-copy-caption>Copy</button>
          <span class="pi-warn"></span>
          <span class="count"></span>
        </div>
        <div class="pi-options"></div>
      </div>
    </div>`).join("");
  $("#pd-items").querySelectorAll("textarea").forEach((t) => t.addEventListener("input", syncPostDialog));
  $("#post-dialog").showModal();
  syncPostDialog();

  try {
    pd.creator = await api("/api/tiktok/creator");
    const c = pd.creator;
    $("#pd-creator").innerHTML = `${c.creator_avatar_url ? `<img src="${esc(c.creator_avatar_url)}" alt="" onerror="this.remove()">` : ""}
      <span>Posting to <b>${esc(c.creator_nickname || "your account")}</b>${c.creator_username ? ` (@${esc(c.creator_username)})` : ""}</span>`;
    const sel = $("#pd-privacy");
    sel.innerHTML = '<option value="" disabled selected>Select who can view…</option>' +
      (c.privacy_level_options || []).map((o) => `<option value="${esc(o)}">${esc(PRIVACY_LABELS[o] || o)}</option>`).join("");
    for (const [id, off] of [["pd-comment", c.comment_disabled], ["pd-duet", c.duet_disabled], ["pd-stitch", c.stitch_disabled]]) {
      $("#" + id).checked = false;
      $("#" + id).disabled = !!off;
      $("#" + id).closest("label").title = off ? "Turned off in your TikTok privacy settings" : "";
    }
  } catch (err) {
    pd.creator = null;
    $("#pd-creator").textContent = "";
    $("#pd-error").textContent = `Can't post right now: ${err.message}`;
    $("#pd-error").classList.remove("hidden");
  }
  syncPostDialog();
}

function syncPostDialog() {
  if (!pd) return;
  const f = $("#post-form");
  const mode = f.querySelector('input[name="pd-mode"]:checked').value;
  const direct = mode === "direct";
  $("#pd-direct-settings").classList.toggle("hidden", !direct);
  $("#pd-draft-note").classList.toggle("hidden", direct);

  // commercial content disclosure
  const disclose = $("#pd-disclose").checked, own = $("#pd-brand-own"), paid = $("#pd-brand-paid");
  $("#pd-disclose-opts").classList.toggle("hidden", !disclose);
  if (!disclose) { own.checked = false; paid.checked = false; }
  const privacy = $("#pd-privacy");
  const selfOnly = privacy.querySelector('option[value="SELF_ONLY"]');
  if (selfOnly) {
    selfOnly.disabled = paid.checked;
    selfOnly.title = paid.checked ? "Branded content visibility cannot be set to private." : "";
    selfOnly.textContent = paid.checked ? "Only me (not available for branded content)" : "Only me";
  }
  paid.disabled = privacy.value === "SELF_ONLY";
  paid.closest("label").title = paid.disabled ? "Branded content visibility cannot be set to private." : "";
  $("#pd-label-note").textContent = paid.checked ? "Your video will be labeled as 'Paid partnership'."
    : own.checked ? "Your video will be labeled as 'Promotional content'." : "";
  $("#pd-declaration").innerHTML = direct && paid.checked && disclose
    ? `By posting, you agree to TikTok's ${BC_LINK} and ${MUSIC_LINK}.`
    : `By posting, you agree to TikTok's ${MUSIC_LINK}.`;

  // scheduling
  const later = $("#pd-when").value === "later";
  $("#pd-at-wrap").classList.toggle("hidden", !later);

  // per-clip checks
  let problem = "";
  const maxDur = pd.creator && pd.creator.max_video_post_duration_sec;
  $("#pd-items").querySelectorAll(".pd-item").forEach((el) => {
    const clip = pd.clips.find((c) => c.index === Number(el.dataset.index));
    const len = el.querySelector("textarea").value.length;
    const count = el.querySelector(".count");
    count.textContent = `${len} / 2200`;
    count.classList.toggle("over", len > 2200);
    const tooLong = maxDur && clip.duration > maxDur;
    el.querySelector(".pi-warn").textContent = tooLong ? `Too long: this account can post up to ${maxDur} s` : "";
    if (tooLong) problem = "One or more clips are longer than your TikTok account allows.";
    if (len > 2200) problem = "A caption is longer than 2,200 characters.";
  });
  const btn = $("#pd-submit");
  btn.title = "";
  if (!pd.creator) problem = problem || "Couldn't load your TikTok account.";
  else if (direct && !privacy.value) problem = "Choose who can view the video.";
  else if (direct && disclose && !own.checked && !paid.checked) {
    problem = "You need to indicate if your content promotes yourself, a third party, or both.";
  }
  if (later && !$("#pd-at").value) problem = "Pick a date and time.";
  btn.disabled = !!problem;
  btn.title = problem;
  const n = pd.clips.length;
  btn.textContent = later || (n > 1 && Number($("#pd-interval").value) > 0)
    ? (direct ? `Schedule ${n > 1 ? n + " posts" : "post"}` : `Schedule ${n > 1 ? n + " drafts" : "draft"}`)
    : (direct ? (n > 1 ? `Post ${n} clips` : "Post") : (n > 1 ? `Send ${n} to drafts` : "Send to drafts"));
}

$("#post-form").addEventListener("change", syncPostDialog);

$("#pd-items").addEventListener("click", async (e) => {
  const item = e.target.closest(".pd-item");
  if (!item) return;
  const ta = item.querySelector("textarea");
  if (e.target.closest("[data-copy-caption]")) {
    try { await navigator.clipboard.writeText(ta.value); toast("Caption copied"); } catch (_) { toast("Copy failed"); }
    return;
  }
  const opt = e.target.closest(".pi-option");
  if (opt) { ta.value = opt.dataset.text; syncPostDialog(); return; }
  const smart = e.target.closest("[data-smart]");
  if (!smart) return;
  smart.disabled = true;
  smart.textContent = "✨ Writing…";
  try {
    const { options } = await jsonPost("/api/tiktok/captions", { job_id: pd.job.id, index: Number(item.dataset.index) });
    item.querySelector(".pi-options").innerHTML = '<span class="hint">Pick one (you can still edit it):</span>' +
      options.map((o) => {
        const text = [o.caption, o.hashtags.join(" ")].filter(Boolean).join("\n\n");
        return `<button type="button" class="pi-option" data-text="${esc(text)}">${esc(o.caption)} <span class="tags">${esc(o.hashtags.join(" "))}</span></button>`;
      }).join("");
  } catch (err) { toast(err.message); }
  smart.disabled = false;
  smart.textContent = "✨ Smart caption";
});

$("#post-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  syncPostDialog();
  const btn = $("#pd-submit");
  if (btn.disabled) return toast(btn.title);
  const f = $("#post-form");
  const mode = f.querySelector('input[name="pd-mode"]:checked').value;
  const later = $("#pd-when").value === "later";
  const disclose = $("#pd-disclose").checked;
  const body = {
    mode, consent: true,
    items: [...$("#pd-items").querySelectorAll(".pd-item")].map((el) => ({
      job_id: pd.job.id, index: Number(el.dataset.index), title: el.querySelector("textarea").value })),
    privacy_level: mode === "direct" ? $("#pd-privacy").value : null,
    allow_comment: $("#pd-comment").checked, allow_duet: $("#pd-duet").checked, allow_stitch: $("#pd-stitch").checked,
    brand_organic: disclose && $("#pd-brand-own").checked, branded_content: disclose && $("#pd-brand-paid").checked,
    start_at: later ? new Date($("#pd-at").value).getTime() / 1000 : null,
    interval_minutes: pd.clips.length > 1 ? Number($("#pd-interval").value) : 0,
  };
  btn.disabled = true;
  try {
    const created = await jsonPost("/api/tiktok/posts", body);
    $("#post-dialog").close();
    const first = created[0];
    toast(first.scheduled_at * 1000 > Date.now() + 30000
      ? `Scheduled ${created.length} post${created.length > 1 ? "s" : ""}. The first goes out ${when(first.scheduled_at)}`
      : `Posting to TikTok… It may take a few minutes to appear on your profile.`);
    pollPosts();
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
  }
});

// returning from TikTok's login page
(() => {
  const q = new URLSearchParams(location.search);
  if (q.has("tiktok") || q.has("tiktok_error")) {
    history.replaceState(null, "", "/");
    if (q.get("tiktok") === "connected") toast("TikTok connected! Use “Post to TikTok” on any clip.");
    else toast(`TikTok connection failed: ${q.get("tiktok_error")}`);
    setTimeout(() => $("#open-tiktok").click(), 300);
  }
})();

refreshStatus();
refreshTikTok();
poll(true).then(pollPosts);
