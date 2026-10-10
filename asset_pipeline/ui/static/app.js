// Terraformer Pipeline UI: data loading, shared helpers and stage 0 (Style). State lives on
// the server; this re-renders from it. The window chrome (menus, tabs, side panel, job
// dialogs) is in shell.js; stage 1 (Plan) is in plan.js.
const $ = (s, el = document) => el.querySelector(s);
const URL_ARGS = new URLSearchParams(location.search);
let slug = null, data = null, statusData = null, plans = [], refs = [], reviewData = null, siteData = null, framesData = null,
  selectedScene = null, polling = null;

async function api(path, body, method = "POST") {
  const r = await fetch(path, body === undefined ? {} : body instanceof FormData ? {method, body} : {
    method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = j.detail;  // FastAPI: a string, or a list of pydantic validation errors
    throw new Error(Array.isArray(d) ? d.map(e => `${e.loc.filter(x => x !== "body").join(" › ")}: ${e.msg}`).join("\n")
      : d || r.statusText);
  }
  // every job this UI starts gets its progress dialog (loaders.js)
  if (method === "POST" && j && typeof j.id === "number" && j.kind && j.status && typeof openJobDialog === "function")
    setTimeout(() => openJobDialog(j.id), 0);
  return j;
}

// Every image URL carries a version, so a re-rendered pass, preview or plan always shows:
// by default the time the last UI job finished (renders, builds, generations all run as
// jobs); callers pass a finer one where they know it (a pass's render time). The server
// also marks files no-cache, so renders run from the CLI show on the next refresh.
let fileVersion = 0;
const fileUrl = (key, v) => `/files/${slug}/${key}?v=${encodeURIComponent(v ?? fileVersion)}`;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

function card(key, {label, onSelect, selected} = {}) {
  const starred = !!data.stars[key];
  const el = document.createElement("div");
  el.className = "card" + (starred ? " starred" : "") + (selected ? " selected" : "");
  el.innerHTML = `<a href="${fileUrl(key)}" target="_blank"><img loading="lazy" alt="" src="${fileUrl(key)}"></a>
    <button class="star" title="${starred ? "Unstar" : "Star"}" aria-pressed="${starred}">${starred ? "★" : "☆"}</button>
    ${label ? `<span class="label">${label}</span>` : ""}`;
  $(".star", el).onclick = async () => {
    await api(`/api/projects/${slug}/star`, {path: key, starred: !starred});
    await load();
  };
  if (onSelect) { $("img", el).parentElement.onclick = e => { e.preventDefault(); onSelect(key); }; }
  return el;
}

// --- jobs and Stop buttons -------------------------------------------------------------
// Every long action is a server job with a kind and a tag (scene, unit, asset...). A Stop
// button cancels only the active jobs of its own action: the server drops queued ones,
// removes or interrupts their ComfyUI prompts, and kills TRELLIS cleanly.
const isActive = j => j.status === "queued" || j.status === "running";
const JOB_LABELS = {"style.explore": "scene generation", "style.derive": "derive", "plan.analyze": "scene analysis",
  "plan.refine": "box refinement", "refs.generate": "reference sheets", "views.cut": "cutting views",
  "3d.trellis": "3D (TRELLIS)", "cleanup": "cleanup (Blender)", "site.build": "greybox build",
  "site.extract": "reading the .blend", "site.preview": "aerial previews", "shots.render": "shot passes",
  "frames.generate": "concept frames", "style.draft": "style text draft", "site.plan": "city plan",
  "shots.camera": "moving a camera", "ue.export": "export to Unreal", "ue.backup": "UE backup"};
// Analysis runs inside the VLM server and can't be interrupted mid-request: no Stop for it.
const STOPPABLE = kind => kind !== "plan.analyze";

function activeJobs(kind, match = {}) {
  return (data ? data.jobs : []).filter(j => j.kind === kind && isActive(j) &&
    Object.entries(match).every(([k, v]) => j.tag[k] === v));
}

async function cancelJobs(jobs, button) {
  if (button) { button.disabled = true; button.textContent = "Stopping…"; }
  try { await Promise.all(jobs.map(j => api(`/api/jobs/${j.id}/cancel`, {}))); }
  catch (err) { alert(err.message); }
  await load();
}

function setStop(sel, jobs) {
  const b = $(sel);
  b.hidden = !jobs.length; b.disabled = false; b.textContent = "Stop";
  b.onclick = () => cancelJobs(jobs, b);
}

function stopButton(jobs) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "stop"; b.textContent = "Stop"; b.hidden = !jobs.length;
  b.onclick = () => cancelJobs(jobs, b);
  return b;
}

function render() {
  const p = data.project;
  $("#empty").hidden = true;
  const brief = $("#explore-form [name=brief]");
  if (!brief.value) brief.value = p.brief || "";

  const batches = $("#batches"); batches.replaceChildren();
  for (const b of [...data.explore].reverse()) {
    const sec = document.createElement("div"); sec.className = "batch";
    sec.innerHTML = `<h3>${esc(b.name)}</h3>`;
    const g = document.createElement("div"); g.className = "grid";
    b.images.forEach(k => g.append(card(k)));
    sec.append(g); batches.append(sec);
  }

  const scenes = Object.keys(data.stars).filter(k => k.startsWith("style/explore/")).sort();
  const board = Object.values(data.moodboard || {}).flat();  // moodboard images can be derived from too
  if (!scenes.includes(selectedScene) && !board.includes(selectedScene)) selectedScene = scenes[0] || null;
  const ss = $("#starred-scenes"); ss.replaceChildren();
  if (!scenes.length) ss.innerHTML = `<p class="hint">Star a scene above first.</p>`;
  scenes.forEach(k => ss.append(card(k, {selected: k === selectedScene,
    onSelect: key => { selectedScene = key; render(); }})));
  $("#derive-form button").disabled = !selectedScene;

  const derived = $("#derived"); derived.replaceChildren();
  for (const d of data.derived) {
    const sec = document.createElement("div"); sec.className = "batch";
    sec.innerHTML = `<h3>from ${esc(d.scene)}</h3>`;
    const g = document.createElement("div"); g.className = "grid small";
    // pair each cutout with its redraw: sort by object name, cutout first
    const objKey = k => k.split("/").pop().replace(/^(cut|obj)_/, "") + (k.includes("/cut_") ? "0" : "1");
    d.images.filter(k => !k.endsWith("meta.json")).sort((x, y) => objKey(x).localeCompare(objKey(y))).forEach(k => {
      const name = k.split("/").pop();
      g.append(card(k, {label: name.startsWith("obj_") ? "redraw" : "cutout"}));
    });
    sec.append(g); derived.append(sec);
  }

  const a = p.anchor;
  const st = $("#anchor-form [name=style_text]");
  if (a && document.activeElement !== st && !st.dataset.touched) st.value = a.style_text || "";
  const sl = $("#anchor-form [name=strength]");
  if (a && !sl.dataset.touched) { sl.value = a.strength; $("#strength-out").value = (+a.strength).toFixed(2); }
  const cur = $("#anchor-current");
  cur.innerHTML = a ? `<p class="hint">Current anchor: ${a.images.length} image(s), strength ${a.strength}${a.lora ? `, LoRA ${esc(a.lora.file)}` : ""}.</p>` :
    `<p class="hint">No anchor saved yet.</p>`;
  if (a) {
    const g = document.createElement("div"); g.className = "grid small";
    a.images.forEach(path => {
      const key = path.split(`/projects/${slug}/`).pop();
      const el = document.createElement("div"); el.className = "card";
      el.innerHTML = `<img alt="" src="${fileUrl(key)}">`; g.append(el);
    });
    cur.append(g);
  }

  renderSite();
  renderCatalog();
  renderFrames();
  renderMoodboard();
  renderUnreal();
  renderPlan();
  renderRefs();
  renderReview();

  renderShell();
  setStop("#explore-stop", activeJobs("style.explore"));
  setStop("#derive-stop", activeJobs("style.derive"));
}

async function load() {
  if (!slug) { $("#empty").hidden = false; renderShell(); return; }
  [data, plans, refs, reviewData, siteData, framesData, catalogData, statusData] = await Promise.all([api(`/api/projects/${slug}`),
    api(`/api/projects/${slug}/plans`), api(`/api/projects/${slug}/refs`), api(`/api/projects/${slug}/review`),
    api(`/api/projects/${slug}/site`), api(`/api/projects/${slug}/frames`), api(`/api/projects/${slug}/catalog`),
    api(`/api/projects/${slug}/status`)]);
  unrealData = await api(`/api/projects/${slug}/unreal`);
  fileVersion = Math.max(0, ...data.jobs.map(j => j.finished || 0));
  render();
}

async function loadProjects(pick) {
  const list = await api("/api/projects");
  const sel = $("#project");
  sel.innerHTML = list.map(s => `<option>${esc(s)}</option>`).join("");
  slug = pick || URL_ARGS.get("project") || localStorage.getItem("ap.project") || list[0] || null;
  if (slug && !list.includes(slug)) slug = list[0] || null;
  if (slug) sel.value = slug;
  await load();
}

$("#project").onchange = e => {
  if (!confirmDiscard()) { e.target.value = slug; return; }
  slug = e.target.value; localStorage.setItem("ap.project", slug); load(); };

$("#explore-form").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api(`/api/projects/${slug}/style/explore`, {brief: f.get("brief") || null,
      n: +f.get("n"), seed: f.get("seed") === "" ? null : +f.get("seed")});
    await load();
  } catch (err) { alert(err.message); }
};

$("#derive-form").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api(`/api/projects/${slug}/style/derive`, {scene: selectedScene,
      nouns: f.get("nouns").split(",").map(s => s.trim()).filter(Boolean),
      per_noun: +f.get("per_noun"), style_text: $("#anchor-form [name=style_text]").value});
    await load();
  } catch (err) { alert(err.message); }
};

const strength = $("#anchor-form [name=strength]");
strength.oninput = () => { strength.dataset.touched = "1"; $("#strength-out").value = (+strength.value).toFixed(2); };
$("#anchor-form [name=style_text]").oninput = e => { e.target.dataset.touched = "1"; };
$("#anchor-form").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api(`/api/projects/${slug}/style/anchor`, {strength: +f.get("strength"),
      style_text: f.get("style_text")});
    delete e.target.style_text.dataset.touched; delete strength.dataset.touched;
    await load();
  } catch (err) { alert(err.message); }
};


// --- moodboard (Style tab) ------------------------------------------------------------------
let draftApplied = Number(sessionStorage.getItem("ap.draftApplied") || 0);

function renderMoodboard() {
  const el = $("#moodboard-groups"); el.replaceChildren();
  for (const [group, keys] of Object.entries(data.moodboard || {})) {
    const sec = document.createElement("div"); sec.className = "batch";
    sec.innerHTML = `<h3>${esc(group)} (${keys.length})</h3>`;
    const g = document.createElement("div"); g.className = "grid small";
    keys.forEach(k => g.append(card(k, {onSelect: key => { selectedScene = key; render(); }, selected: k === selectedScene})));
    sec.append(g); el.append(sec);
  }
  const drafting = activeJobs("style.draft");
  $("#style-draft").disabled = !Object.keys(data.moodboard || {}).length || !!drafting.length;
  $("#style-draft").textContent = drafting.length ? "Drafting…" : "Draft style text from moodboard";
  setStop("#style-draft-stop", drafting);
  // a finished draft fills the style text box once (it's saved only with the anchor buttons)
  const done = data.jobs.filter(j => j.kind === "style.draft" && j.status === "done" && j.id > draftApplied)
    .sort((a, b) => b.id - a.id)[0];
  if (done) {
    const st = $("#anchor-form [name=style_text]");
    st.value = done.result; st.dataset.touched = "1";
    draftApplied = done.id; sessionStorage.setItem("ap.draftApplied", draftApplied);
  }
}

$("#moodboard-form").onsubmit = async e => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("group", e.target.group.value);
  for (const file of e.target.files.files) fd.append("files", file);
  try { await api(`/api/projects/${slug}/moodboard`, fd); e.target.reset(); }
  catch (err) { alert(err.message); }
  await load();
};
$("#style-draft").onclick = async () => {
  try { await api(`/api/projects/${slug}/style/draft`, {}); } catch (err) { alert(err.message); }
  await load();
};
$("#anchor-text-only").onclick = async () => {
  const st = $("#anchor-form [name=style_text]");
  try { await api(`/api/projects/${slug}/style/text`, {text: st.value}); delete st.dataset.touched; }
  catch (err) { alert(err.message); }
  await load();
};
