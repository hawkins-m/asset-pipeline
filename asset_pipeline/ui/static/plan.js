// Stage 1 (Plan): analyse a scene with the vision LLM, then edit the asset plan.
// Edits go into a local draft; the server copy only replaces it when there are no
// unsaved changes, so the 2 s job polling never clobbers typing.
const CATEGORIES = ["building", "structure", "prop", "vegetation", "rock", "terrain", "vehicle", "other"];
let planScene = null, draft = null, draftOf = null, dirty = false, vlmCheckedAt = 0;

const clone = o => JSON.parse(JSON.stringify(o));
const planEntry = () => plans.find(p => p.scene === planScene);

function confirmDiscard() {
  return !dirty || confirm("Discard unsaved plan changes?");
}

function setDirty(on = true) {
  dirty = on;
  $("#save-plan").disabled = $("#discard-plan").disabled = !on;
  $("#plan-dirty").textContent = on ? "Unsaved changes" : "";
}

async function refreshVlm() {
  if (Date.now() - vlmCheckedAt < 5000) return;
  vlmCheckedAt = Date.now();
  const llm = data.project.llm;
  const el = $("#vlm-status");
  if (llm !== "local") {
    el.textContent = `Vision LLM: ${llm} (paid API, blocked unless the UI was started with AP_ALLOW_PAID_APIS=1).`;
    return;
  }
  const s = await api("/api/vlm").catch(e => ({up: false, error: e.message}));
  el.innerHTML = s.up ? `Vision LLM: local Qwen3-VL, server up (model ${s.loaded ? "loaded" : "loads on first request"}).`
    : `Vision LLM: local Qwen3-VL, <strong>server not running</strong>. Start it with <code>ap vlm up</code>.`;
}

function renderPlan() {
  refreshVlm();
  const keys = plans.map(p => p.scene);
  if (!keys.includes(planScene)) planScene = keys[0] || null;

  const list = $("#plan-scene-list"); list.replaceChildren();
  if (!plans.length) list.innerHTML = `<p class="hint">Star a scene in the Style tab, or upload one.</p>`;
  for (const p of plans) {
    const label = p.plan ? `${p.plan.assets.length} assets${p.plan.edited ? " · edited" : ""}` : "not analysed";
    const el = card(p.scene, {label, selected: p.scene === planScene, onSelect: key => {
      if (key === planScene || !confirmDiscard()) return;
      planScene = key; setDirty(false); draft = null; renderPlan();
    }});
    $(".star", el).remove();  // stars belong to the Style tab
    list.append(el);
  }

  const busy = data.jobs.some(j => j.kind.startsWith("plan.") && (j.status === "queued" || j.status === "running"));
  const entry = planEntry();
  const btn = $("#analyze-btn");
  btn.disabled = !entry || busy;
  btn.textContent = busy ? "Working…" : entry && entry.plan ? "Re-analyse scene" : "Analyse scene";
  $("#refine-btn").disabled = !(entry && entry.plan) || busy;

  $("#plan-editor").hidden = !(entry && entry.plan);
  if (!entry || !entry.plan) { draft = null; return; }
  // Take the server copy when nothing is unsaved, or when it changed under us (re-analysis).
  const stamp = entry.plan.edited || entry.plan.created;
  if (!draft || (!dirty && draftOf !== stamp)) {
    draft = clone(entry.plan); draftOf = stamp; setDirty(false); renderEditor();
  }
}

function renderEditor() {
  const entry = planEntry();
  $("#plan-summary").value = draft.summary;
  $("#plan-scale").value = draft.scale_notes;
  $("#plan-meta").textContent = `Drafted by ${draft.llm} on ${new Date(draft.created).toLocaleString()}` +
    (draft.edited ? `, edited ${new Date(draft.edited).toLocaleString()}` : "") + `. File: plan/${entry.name}.json`;

  const fig = $("#plan-figure");
  fig.innerHTML = `<img alt="Scene ${esc(entry.scene)}" src="${fileUrl(entry.scene)}">`;
  draft.assets.forEach((a, i) => {
    if (!a.bbox) return;
    const [x0, y0, x1, y1] = a.bbox;
    const box = document.createElement("button");
    box.type = "button"; box.className = `bbox ${a.bbox_source}`; box.dataset.i = i;
    box.title = a.name; box.setAttribute("aria-label", `Show ${a.name}`);
    Object.assign(box.style, {left: `${x0 * 100}%`, top: `${y0 * 100}%`,
      width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`});
    box.onclick = () => { const c = $(`.asset[data-i="${i}"]`); c.scrollIntoView({behavior: "smooth", block: "center"}); highlight(i); };
    fig.append(box);
  });

  const list = $("#plan-asset-list"); list.replaceChildren();
  draft.assets.forEach((a, i) => list.append(assetCard(a, i)));
  renderRelations();
}

function highlight(i) {
  document.querySelectorAll(".bbox, .asset").forEach(el => el.classList.toggle("active", +el.dataset.i === i));
}

function assetCard(a, i) {
  const el = document.createElement("div");
  el.className = "asset" + (a.include ? "" : " excluded"); el.dataset.i = i;
  const d = a.dimensions;
  el.innerHTML = `
    <div class="asset-head">
      <label class="check"><input type="checkbox" data-f="include" ${a.include ? "checked" : ""}> Include</label>
      <input data-f="name" value="${esc(a.name)}" aria-label="Name" class="name">
      <span class="id" title="Stable id (file names use it)">${esc(a.id || "new")}</span>
      ${a.sam_found == null ? "" : `<span class="sam" title="Copies SAM 3.1 found for this name (at most 8). The box is ${a.bbox_source === "sam" ? "SAM's" : "the LLM's"}.">SAM: ${a.sam_found || "none"}</span>`}
      <button type="button" class="remove" title="Delete asset" aria-label="Delete ${esc(a.name)}">×</button>
    </div>
    <div class="asset-fields">
      <label title="Plain noun SAM 3.1 looks for when refining the box">Segment as <input data-f="noun" value="${esc(a.noun || "")}" placeholder="${esc(a.name)}"></label>
      <label>Category <select data-f="category">${CATEGORIES.map(c => `<option ${c === a.category ? "selected" : ""}>${c}</option>`).join("")}</select></label>
      <label>Count <input data-f="count" type="number" min="1" step="1" value="${a.count}"></label>
      <label>Width m <input data-f="dimensions.width" type="number" min="0.01" step="any" value="${d.width}"></label>
      <label>Depth m <input data-f="dimensions.depth" type="number" min="0.01" step="any" value="${d.depth}"></label>
      <label>Height m <input data-f="dimensions.height" type="number" min="0.01" step="any" value="${d.height}"></label>
      <label>Kit <input data-f="kit" value="${esc(a.kit || "")}" placeholder="none" list="kit-names"></label>
      <label>Use <select data-f="usage"><option ${a.usage === "game" ? "selected" : ""}>game</option><option ${a.usage === "cine" ? "selected" : ""}>cine</option></select></label>
    </div>
    <label>Description <textarea data-f="description" rows="2">${esc(a.description)}</textarea></label>
    <label>Placement <input data-f="placement" value="${esc(a.placement)}"></label>`;
  el.addEventListener("input", e => {
    const f = e.target.dataset.f;
    if (!f) return;
    let v = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    if (e.target.type === "number") v = e.target.value === "" ? null : +e.target.value;
    if (f === "kit") v = v.trim() || null;
    const [k, sub] = f.split(".");
    if (sub) a[k][sub] = v; else a[k] = v;
    if (f === "include") el.classList.toggle("excluded", !v);
    if (f === "name" || f === "kit") renderRelations();
    setDirty();
  });
  el.addEventListener("focusin", () => highlight(i));
  el.addEventListener("mouseenter", () => highlight(i));
  $(".remove", el).onclick = () => {
    draft.relations = draft.relations.filter(r => r.subject !== a.id && r.object !== a.id);
    draft.assets.splice(i, 1); setDirty(); renderEditor();
  };
  return el;
}

function renderRelations() {
  const name = id => (draft.assets.find(a => a.id === id) || {name: id}).name;
  const ul = $("#plan-relations"); ul.replaceChildren();
  if (!draft.relations.length) ul.innerHTML = `<li class="hint">None.</li>`;
  draft.relations.forEach((r, i) => {
    const li = document.createElement("li");
    li.innerHTML = `${esc(name(r.subject))} <em>${esc(r.relation)}</em> ${esc(name(r.object))}
      <button type="button" class="remove" aria-label="Delete relation">×</button>`;
    $(".remove", li).onclick = () => { draft.relations.splice(i, 1); setDirty(); renderRelations(); };
    ul.append(li);
  });
  // Relations need ids; assets added in the editor get theirs on save.
  const opts = draft.assets.filter(a => a.id).map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
  $("#relation-form [name=subject]").innerHTML = opts;
  $("#relation-form [name=object]").innerHTML = opts;
  let dl = $("#kit-names");
  if (!dl) { dl = document.createElement("datalist"); dl.id = "kit-names"; document.body.append(dl); }
  dl.innerHTML = [...new Set(draft.assets.map(a => a.kit).filter(Boolean))].map(k => `<option value="${esc(k)}">`).join("");
}

$("#relation-form").onsubmit = e => {
  e.preventDefault();
  const f = new FormData(e.target);
  if (f.get("subject") === f.get("object")) { alert("Pick two different assets."); return; }
  draft.relations.push({subject: f.get("subject"), relation: f.get("relation").trim(), object: f.get("object")});
  e.target.relation.value = ""; setDirty(); renderRelations();
};

$("#plan-summary").oninput = e => { draft.summary = e.target.value; setDirty(); };
$("#plan-scale").oninput = e => { draft.scale_notes = e.target.value; setDirty(); };

$("#add-asset").onclick = () => {
  draft.assets.push({id: "", name: "new asset", noun: "", category: "prop", description: "", count: 1,
    dimensions: {width: 1, depth: 1, height: 1}, kit: null, placement: "", bbox: null,
    usage: "game", include: true});
  setDirty(); renderEditor();
  const last = $("#plan-asset-list").lastElementChild;
  last.scrollIntoView({behavior: "smooth", block: "center"}); $("[data-f=name]", last).select();
};

$("#discard-plan").onclick = () => {
  if (!confirmDiscard()) return;
  draft = null; setDirty(false); renderPlan();
};

$("#save-plan").onclick = async () => {
  const entry = planEntry();
  try {
    const saved = await api(`/api/projects/${slug}/plans/${entry.name}`, draft, "PUT");
    entry.plan = saved; draft = clone(saved); draftOf = saved.edited;
    setDirty(false); renderEditor(); renderPlan();
  } catch (err) { alert(`Not saved: ${err.message}`); }
};

$("#analyze-btn").onclick = async () => {
  const entry = planEntry();
  if (!entry || !confirmDiscard()) return;
  const go = force => api(`/api/projects/${slug}/plans/analyze`, {scene: entry.scene, force});
  try {
    try { await go(false); } catch (err) {
      if (!/has edits/.test(err.message)) throw err;
      if (!confirm("This plan has edits. Re-analysing replaces them (the old plan is kept as .prev.json). Continue?")) return;
      await go(true);
    }
    setDirty(false); draft = null;
    await load();
  } catch (err) { alert(err.message); }
};

$("#refine-btn").onclick = async () => {
  const entry = planEntry();
  if (dirty) { alert("Save or discard your changes first: refining rewrites the plan's boxes."); return; }
  try { await api(`/api/projects/${slug}/plans/${entry.name}/refine`, {}); await load(); }
  catch (err) { alert(err.message); }
};

$("#scene-upload").onchange = async e => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await api(`/api/projects/${slug}/scenes`, fd);
    if (confirmDiscard()) { planScene = r.scene; setDirty(false); draft = null; }
    await load();
  } catch (err) { alert(err.message); }
  e.target.value = "";
};

window.addEventListener("beforeunload", e => { if (dirty) e.preventDefault(); });
