// Asset pipeline UI: shell and stage 0 (Style). State lives on the server; this re-renders
// from it. Stage 1 (Plan) is in plan.js.
const $ = (s, el = document) => el.querySelector(s);
let slug = null, data = null, plans = [], selectedScene = null, polling = null;

async function api(path, body, method = "POST") {
  const r = await fetch(path, body === undefined ? {} : body instanceof FormData ? {method, body} : {
    method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = j.detail;  // FastAPI: a string, or a list of pydantic validation errors
    throw new Error(Array.isArray(d) ? d.map(e => `${e.loc.filter(x => x !== "body").join(" › ")}: ${e.msg}`).join("\n")
      : d || r.statusText);
  }
  return j;
}

const fileUrl = key => `/files/${slug}/${key}`;
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
  if (!scenes.includes(selectedScene)) selectedScene = scenes[0] || null;
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

  renderPlan();

  const active = data.jobs.filter(j => j.status === "queued" || j.status === "running");
  const failed = data.jobs.filter(j => j.status === "error").slice(-1);
  $("#jobs").innerHTML = active.length ? `${active.length} job(s) running: ${active.map(j => j.kind).join(", ")}…` :
    failed.length ? `<span class="error">Last job failed: ${esc(failed[0].error)}</span>` : "";
  if (active.length && !polling) polling = setInterval(load, 2000);
  if (!active.length && polling) { clearInterval(polling); polling = null; }
}

async function load() {
  if (!slug) { $("#empty").hidden = false; return; }
  [data, plans] = await Promise.all([api(`/api/projects/${slug}`), api(`/api/projects/${slug}/plans`)]);
  render();
}

async function loadProjects(pick) {
  const list = await api("/api/projects");
  const sel = $("#project");
  sel.innerHTML = list.map(s => `<option>${esc(s)}</option>`).join("");
  slug = pick || localStorage.getItem("ap.project") || list[0] || null;
  if (slug && !list.includes(slug)) slug = list[0] || null;
  if (slug) sel.value = slug;
  await load();
}

function showTab(tab) {
  document.querySelectorAll("nav [data-tab]").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === tab));
  document.querySelectorAll("main[data-tab]").forEach(m => { m.hidden = m.dataset.tab !== tab; });
  localStorage.setItem("ap.tab", tab);
}
document.querySelectorAll("nav [data-tab]").forEach(b => { b.onclick = () => showTab(b.dataset.tab); });
showTab(localStorage.getItem("ap.tab") === "plan" ? "plan" : "style");

$("#project").onchange = e => {
  if (!confirmDiscard()) { e.target.value = slug; return; }
  slug = e.target.value; localStorage.setItem("ap.project", slug); load(); };

$("#new-form").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/projects", {slug: f.get("slug"), brief: f.get("brief")});
    localStorage.setItem("ap.project", f.get("slug"));
    e.target.reset(); $("#new-project").open = false;
    await loadProjects(f.get("slug"));
  } catch (err) { alert(err.message); }
};

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

loadProjects();
