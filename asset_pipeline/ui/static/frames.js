// Frames tab: review concept frames shot by shot. Shot tree (side panel), one large viewer
// (frame, greybox, depth, edges or split), a filmstrip per batch, and per frame: what it's
// used as (shot frame = asset-library source, design reference, reject), star, the
// typologies it is the landmark reference for, and a note for the next round.
// Per-frame edits are a draft until Save and next / Ctrl+S, and are saved when moving on.
const ROLE_LABELS = {source: "Shot frame (feeds the library)", design_ref: "Design reference (look only)", rejected: "Reject"};
const TIER_ORDER = ["wide", "medium", "tight"];
const fr = {shot: null, batch: null, frame: 0, view: "frame", filter: "all", draft: null, shown: ""};
try { fr.view = localStorage.getItem("ap.frView") || "frame"; } catch (_) { /* ignore */ }
const framesPerShot = () => { try { return +localStorage.getItem("ap.framesN") || 4; } catch (_) { return 4; } };

// --- shot state --------------------------------------------------------------------------
const shotRows = () => framesData ? framesData.shots : [];
const shotById = id => shotRows().find(r => r.id === id);
const currentBatches = r => r.batches.filter(b => !b.archived);
const allFrames = r => currentBatches(r).flatMap(b => b.frames);
function shotMark(r) {
  const fs = allFrames(r), stars = fs.filter(f => data.stars[f.key]).length;
  const newest = currentBatches(r).at(-1);
  const fresh = newest ? newest.frames.filter(f => !f.role).length : 0;
  if (!r.rendered || r.stale || (fs.length && fs.every(f => f.role === "rejected"))) return ["redo", "stale"];
  if (fresh) return [`${fresh} new`, "new"];
  return stars ? [`★${stars}`, "star"] : ["", ""];
}
const FILTERS = {all: ["All shots", () => true], review: ["Needs review", r => allFrames(r).some(f => !f.role)],
  starred: ["Starred", r => allFrames(r).some(f => data.stars[f.key])], redo: ["Redo", r => shotMark(r)[0] === "redo"],
  none: ["Without frames", r => !r.batches.length]};
const visibleShots = () => shotRows().filter(FILTERS[fr.filter][1]);

function batchList(r) { return [...r.batches].reverse(); }       // newest first
function curBatch(r) { return r.batches.find(b => b.dir === fr.batch) || currentBatches(r).at(-1) || r.batches.at(-1) || null; }
function curFrame(r) { const b = curBatch(r); return b && b.frames[Math.min(fr.frame, b.frames.length - 1)] || null; }

function selectShot(id, frameIndex = 0) {
  if (fr.shot !== id) flushDraft();
  fr.shot = id; fr.batch = null; fr.frame = frameIndex; fr.draft = null; fr.shown = ""; fr.rshown = "";
  try { localStorage.setItem(`ap.frShot.${slug}`, id); } catch (_) { /* ignore */ }
  renderFrames();
}
function selectFrame(i, batch) {
  flushDraft();
  if (batch !== undefined) fr.batch = batch;
  fr.frame = i; fr.draft = null; fr.shown = ""; fr.rshown = "";
  renderFrames();
}

// --- per-frame draft ---------------------------------------------------------------------
function frameDraft(f) {
  if (!fr.draft || fr.draft.key !== f.key) {
    fr.draft = {key: f.key, role: f.role || null, starred: !!data.stars[f.key], note: f.note || "",
      landmark: landmarkTypes(f.key), changed: false};
  }
  return fr.draft;
}
const landmarkTypes = key => (catalogData && catalogData.typologies || []).filter(t => t.ref === key).map(t => t.id);

async function saveDraft(d = fr.draft) {
  if (!d || !d.changed) return true;
  const f = shotRows().flatMap(r => r.batches.flatMap(b => b.frames)).find(x => x.key === d.key);
  try {
    if (!f || (f.role || null) !== d.role || (f.note || "") !== d.note)
      await api(`/api/projects/${slug}/role`, {path: d.key, role: d.role, note: d.note});
    const starred = d.role === "rejected" ? false : d.starred;      // a rejected frame is never starred
    if (!!data.stars[d.key] !== starred) await api(`/api/projects/${slug}/star`, {path: d.key, starred});
    const before = landmarkTypes(d.key);
    if (JSON.stringify([...before].sort()) !== JSON.stringify([...d.landmark].sort())) {
      if (typeof isDirty === "function" && isDirty("typologies") &&
          !confirm("The City tab has unsaved building-type edits; saving the landmark reference reloads them. Continue?")) return false;
      const rows = catalogData.typologies.map(t => d.landmark.includes(t.id) ? {...t, ref: d.key}
        : t.ref === d.key ? {...t, ref: null} : t);
      await api(`/api/projects/${slug}/catalog/typologies`, rows, "PUT");
      noteCatalogSaved();
      if (typeof cityDrafts !== "undefined") delete cityDrafts.typologies;
    }
    d.changed = false;
    return true;
  } catch (err) { alert(err.message); return false; }
}
function flushDraft() { if (fr.draft && fr.draft.changed) { const d = fr.draft; saveDraft(d).then(ok => ok && load()); } }

async function saveAndNext() {
  if (!(await saveDraft())) return;
  fr.draft = null;
  const r = shotById(fr.shot), b = r && curBatch(r);
  if (b && fr.frame < b.frames.length - 1) fr.frame++;
  else {                                   // next shot that still needs review, else the next shot
    const shots = visibleShots(), i = shots.findIndex(x => x.id === fr.shot);
    const nxt = shots.slice(i + 1).find(FILTERS.review[1]) || shots[i + 1];
    if (nxt) { fr.shot = nxt.id; fr.batch = null; fr.frame = 0; }
  }
  fr.shown = ""; fr.rshown = "";
  await load();
}

// --- side panel: shot tree ---------------------------------------------------------------
function renderShotTree() {
  const side = $("#side-frames");
  const rows = shotRows();
  if (!side.dataset.built) {
    side.innerHTML = `<fieldset class="shots-box"><legend>Shots</legend>
        <div class="form" style="margin-bottom:6px"><label for="fr-filter">Show:</label><select id="fr-filter"></select></div>
        <ul class="list tree" id="shot-tree" role="tree" aria-label="Shots" tabindex="0"></ul></fieldset>`;
    side.dataset.built = "1";
    $("#fr-filter").onchange = e => { fr.filter = e.target.value; renderFrames(); };
    $("#shot-tree").onclick = e => { const li = e.target.closest("[data-shot]"); if (li) selectShot(li.dataset.shot); };
  }
  $("#fr-filter").innerHTML = Object.entries(FILTERS).map(([k, [label, fn]]) =>
    `<option value="${k}"${k === fr.filter ? " selected" : ""}>${label} (${rows.filter(fn).length})</option>`).join("");
  const shots = visibleShots(), tree = $("#shot-tree");
  const tiers = [...new Set([...TIER_ORDER, ...shots.map(r => r.tier)])].filter(t => shots.some(r => r.tier === t));
  tree.innerHTML = tiers.map(t => {
    const items = shots.filter(r => r.tier === t);
    return `<li class="tier" role="treeitem" aria-expanded="true"><span>▾ ${esc(t[0].toUpperCase() + t.slice(1))}</span><span class="mono">${items.length}</span></li>` +
      items.map(r => { const [m, cls] = shotMark(r);
        return `<li role="treeitem" data-shot="${esc(r.id)}" aria-selected="${r.id === fr.shot}"><span class="leaf">${esc(r.id)}</span><span class="mark ${cls}">${esc(m)}</span></li>`; }).join("");
  }).join("") || `<li class="hint">No shots match.</li>`;
}

// --- main ----------------------------------------------------------------------------------
function renderFrames() {
  if (!framesData) return;
  renderShotTree();
  const rows = shotRows(), main = $("#frames-main"), right = $("#frames-right");
  if (!rows.length) {
    main.innerHTML = `<p class="hint">No shots yet: build the greybox and render its passes (Site, Shots).</p>`; right.hidden = true; return;
  }
  right.hidden = false;
  if (!shotById(fr.shot)) {
    let last = null; try { last = localStorage.getItem(`ap.frShot.${slug}`); } catch (_) { /* ignore */ }
    fr.shot = (shotById(last) || visibleShots()[0] || rows[0]).id;
  }
  const r = shotById(fr.shot), b = curBatch(r);
  if (b) fr.batch = b.dir;
  if (b && fr.frame >= b.frames.length) fr.frame = Math.max(0, b.frames.length - 1);
  const f = curFrame(r);
  // rebuild only when what's shown changed (the 2 s polling must not reset the note's caret)
  const key = JSON.stringify([r, fr.batch, fr.frame, fr.view, f && data.stars[f.key], f && landmarkTypes(f.key),
    activeJobs("frames.generate", {shot: r.id}).length, activeJobs("shots.camera", {shot: r.id}).length, fileVersion]);
  if (key !== fr.shown) { fr.shown = key; keepFocus(() => renderFrameMain(r, b, f)); }
  // the right panel follows the frame; while its draft has edits, polling leaves it alone
  const rkey = JSON.stringify([r.id, f && f.key, f && f.role, f && f.note, f && data.stars[f.key], f && landmarkTypes(f.key),
    (catalogData && catalogData.typologies || []).length, activeJobs("frames.generate", {shot: r.id}).length]);
  if (rkey !== fr.rshown && !(fr.draft && fr.draft.changed && f && fr.draft.key === f.key)) { fr.rshown = rkey; renderFrameRight(r, f); }
  renderFrameJobs(r);
  const reviewed = rows.flatMap(allFrames);
  statusLeft.frames = () => `Shot ${rows.indexOf(r) + 1} of ${rows.length}`;
  statusRight.frames = () => "S star · 1 2 3 use as · ← → frame · ↑ ↓ shot · Enter save and next";
  statusMid.frames = () => `${reviewed.filter(x => data.stars[x.key]).length} starred · ${reviewed.filter(x => !x.role).length} not reviewed`;
  if (currentTab === "frames") renderShell();
}

const VIEWS = {frame: "Frame", greybox: "Greybox", depth: "Depth", edges: "Edges", split: "Split (frame | greybox)"};
function passUrl(r, which) { return fileUrl({greybox: r.preview, depth: r.depth, edges: r.canny}[which], `${fileVersion}-${r.rendered || ""}`); }

function renderFrameMain(r, b, f) {
  const main = $("#frames-main");
  const bl = batchList(r);
  const c = r.control || framesData.settings;   // this shot's view's settings
  main.innerHTML = `<div class="frames-head">
      <b>${esc(r.id)}</b><span class="hint" style="margin:0">${esc(r.tier)} · ${r.lens_mm} mm${r.district ? ` · ${esc(r.district)} district` : ""}${r.mood ? " · mood only: never seeds assets" : ""}</span>
      <span class="spacer"></span>
      <label>View: <select id="fr-view">${Object.entries(VIEWS).map(([k, v]) => `<option value="${k}"${k === fr.view ? " selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Batch: <select id="fr-batch" ${bl.length ? "" : "disabled"}>${bl.map((x, i) => `<option value="${esc(x.dir)}"${x.dir === (b && b.dir) ? " selected" : ""}>${esc(x.dir.split("_").pop())}${i === 0 && !x.archived ? " (new)" : ""}${x.archived ? " (older layout)" : ""}</option>`).join("")}</select></label>
    </div>
    ${!r.rendered || r.stale ? `<div class="applybar"><span class="msg">This shot's passes are ${r.rendered ? "stale (the greybox or camera changed)" : "missing"}: <em>Regenerate shot</em> re-renders them first.</span></div>` : ""}
    <div class="viewport fr-viewer" id="fr-viewer">${viewerHtml(r, f)}</div>
    <div class="filmstrip" id="fr-strip" role="listbox" aria-label="Frames">
      <button type="button" class="thumb pass" data-view="greybox" title="Greybox pass"><img alt="" src="${passUrl(r, "greybox")}"><span>Greybox</span></button>
      ${b ? b.frames.map((x, i) => `<button type="button" class="thumb${i === fr.frame ? " sel" : ""}${x.role === "rejected" ? " rejected" : ""}" role="option" aria-selected="${i === fr.frame}" data-i="${i}" title="${esc(x.role ? ROLE_LABELS[x.role] : "not reviewed")}">
        <img alt="" loading="lazy" src="${fileUrl(x.key)}"><span>${i + 1} · ${x.edge_match.toFixed(2)}</span>${data.stars[x.key] ? `<span class="tstar">★</span>` : ""}</button>`).join("")
        : `<span class="hint">No frames yet. <em>Regenerate shot</em> makes ${framesPerShot()}.</span>`}
    </div>
    <div class="row" style="margin:0">
      <button type="button" id="fr-prev">◀ Previous</button><button type="button" id="fr-next">Next ▶</button>
      <span class="spacer"></span>
      <span class="mono" id="fr-info">${f ? `layout match ${f.edge_match.toFixed(2)} · seed ${f.seed ?? "?"}` : ""}${b && b.refs.length ? ` · ${b.refs.length} ref${b.refs.length === 1 ? "" : "s"}` : ""}</span>
    </div>
    <p class="hint mono" style="margin:0">${esc(r.view)} view · ${esc(c.model === "union" ? "Union Pro 2.0" : "depth LoRA")}: ${c.depth_image === "relief" ? "relief " : ""}depth ${c.depth_strength}${c.model === "union" ? ` to ${c.depth_end}, canny ${c.canny_strength} to ${c.canny_end}` : ""}, ${c.steps} steps</p>`;
  main.append(shotEditPanel(r));
  $("#fr-view", main).onchange = e => { fr.view = e.target.value; try { localStorage.setItem("ap.frView", fr.view); } catch (_) { /* ignore */ } fr.shown = ""; renderFrames(); };
  $("#fr-batch", main).onchange = e => selectFrame(0, e.target.value);
  $("#fr-strip", main).onclick = e => {
    const t = e.target.closest(".thumb"); if (!t) return;
    if (t.dataset.view) { fr.view = "greybox"; fr.shown = ""; renderFrames(); } else selectFrame(+t.dataset.i);
  };
  $("#fr-prev", main).onclick = () => stepFrame(-1);
  $("#fr-next", main).onclick = () => stepFrame(1);
}

function viewerHtml(r, f) {
  const frameImg = f ? `<img alt="Frame ${fr.frame + 1}" src="${fileUrl(f.key)}">` : `<span class="vp-empty">No frame</span>`;
  if (fr.view === "frame") return frameImg;
  if (fr.view === "split") return `<div class="split">${frameImg}<img alt="Greybox" src="${passUrl(r, "greybox")}"></div>`;
  return `<img alt="${VIEWS[fr.view]}" src="${passUrl(r, fr.view)}">`;
}

function stepFrame(d) {
  const r = shotById(fr.shot), b = r && curBatch(r);
  if (!b || !b.frames.length) return stepShot(d);
  const i = fr.frame + d;
  if (i < 0 || i >= b.frames.length) return stepShot(d, d < 0);
  selectFrame(i);
}
function stepShot(d, toLast = false) {
  const shots = visibleShots(), i = shots.findIndex(x => x.id === fr.shot);
  const n = shots[i + d]; if (!n) return;
  const b = currentBatches(n).at(-1);
  selectShot(n.id, toLast && b ? b.frames.length - 1 : 0);
}

// --- right panel ----------------------------------------------------------------------------
function renderFrameRight(r, f) {
  const box = $("#frames-right");
  const d = f && frameDraft(f);
  const types = (catalogData && catalogData.typologies) || [];
  box.innerHTML = `<fieldset><legend>Use this frame as</legend>
      ${Object.entries(ROLE_LABELS).map(([k, v], i) => `<label class="radio"><input type="radio" name="fr-role" value="${k}"${d && d.role === k ? " checked" : ""} ${f ? "" : "disabled"}> ${v} <kbd>${i + 1}</kbd></label>`).join("")}
      ${d && !d.role ? `<p class="hint" style="margin:4px 0 0">Not reviewed yet.</p>` : ""}
    </fieldset>
    <fieldset><legend>Reference</legend>
      <label class="check"><input type="checkbox" id="fr-star" ${d && d.starred && d.role !== "rejected" ? "checked" : ""} ${f && d.role !== "rejected" ? "" : "disabled"}> Starred</label>
      <div style="margin-top:6px">Landmark reference for:</div>
      <div class="dropdown-pick" id="fr-landmark">
        <button type="button" class="pick" ${f && types.length ? "" : "disabled"} aria-haspopup="true" aria-expanded="false">${d && d.landmark.length ? esc(d.landmark.join(", ")) : "(none)"}</button>
        <div class="pick-list" hidden>${types.map(t => `<label class="check"><input type="checkbox" value="${esc(t.id)}"${d && d.landmark.includes(t.id) ? " checked" : ""}> ${esc(t.id)}${t.ref && t.ref !== (f && f.key) ? ` <span class="hint">(has a ref)</span>` : ""}</label>`).join("")}</div>
      </div>
    </fieldset>
    <fieldset><legend>Note for the next round</legend>
      <textarea id="fr-note" rows="4" data-draft="frnote" ${f ? "" : "disabled"}>${esc(d ? d.note : "")}</textarea>
    </fieldset>
    <div class="row end"><button type="button" id="fr-regen">Regenerate shot</button><button type="button" id="fr-save" class="default" ${f ? "" : "disabled"}>Save and next</button></div>
    <div id="fr-jobs"></div>`;
  const touch = () => { if (fr.draft) fr.draft.changed = true; };
  box.querySelectorAll("[name=fr-role]").forEach(x => x.onchange = () => { d.role = x.value; touch();
    $("#fr-star").disabled = d.role === "rejected"; if (d.role === "rejected") $("#fr-star").checked = false; });
  if (f) {
    $("#fr-star").onchange = e => { d.starred = e.target.checked; touch(); };
    $("#fr-note").oninput = e => { d.note = e.target.value; touch(); };
    const pick = $("#fr-landmark"), list = $(".pick-list", pick), btn = $(".pick", pick);
    btn.onclick = () => { list.hidden = !list.hidden; btn.setAttribute("aria-expanded", !list.hidden); };
    list.onchange = () => { d.landmark = [...list.querySelectorAll("input:checked")].map(x => x.value); btn.textContent = d.landmark.join(", ") || "(none)"; touch(); };
  }
  $("#fr-save").onclick = saveAndNext;
  $("#fr-regen").onclick = () => regenerateDialog(r);
}

function renderFrameJobs(r) {
  const el = $("#fr-jobs"); if (!el) return;
  const jobs = activeJobs("frames.generate", {shot: r.id}), cam = activeJobs("shots.camera", {shot: r.id});
  el.replaceChildren();
  $("#fr-regen").disabled = !!(jobs.length || cam.length);
  if (jobs.length || cam.length) {
    const j = (jobs[0] || cam[0]), p = jobProgress(j);
    el.innerHTML = `<div class="status-ok" style="color:inherit">${j.status === "queued" ? "Queued" : cam.length ? "Moving the camera…" : "Generating…"} ${esc(p.count)}</div>
      ${j.status === "running" ? progressBar(p) : ""}`;
    const row = document.createElement("div"); row.className = "row end"; row.style.marginTop = "6px";
    const det = document.createElement("button"); det.type = "button"; det.textContent = "Details"; det.onclick = () => openJobDialog(j.id);
    row.append(det, stopButton([...jobs, ...cam])); el.append(row);
  }
}

function regenerateDialog(r) {
  const approved = Object.keys(data.stars).filter(k => data.stars[k] && k.startsWith("frames/") && !k.startsWith(`frames/${r.id}/`));
  const f = document.createElement("form"); f.className = "form";
  f.innerHTML = `<label for="rg-n">Frames:</label><input id="rg-n" type="number" min="1" max="12" value="${framesPerShot()}">
    <label for="rg-ref">Extra reference:</label><select id="rg-ref"><option value="">(none)</option>${approved.map(k => `<option>${esc(k)}</option>`).join("")}</select>
    <span></span><p class="hint" style="margin:0">${!r.rendered || r.stale ? "The passes are stale or missing: they are re-rendered first (CPU). " : ""}About 48 s per frame on GPU 1, more with references.</p>`;
  const go = async () => {
    const n = Math.max(1, Math.min(12, +$("#rg-n", f).value || 4)), ref = $("#rg-ref", f).value;
    try { localStorage.setItem("ap.framesN", n); } catch (_) { /* ignore */ }
    try { const j = await api(`/api/projects/${slug}/shots/${r.id}/regenerate`, {n, refs: ref ? [ref] : []}); await load(); openJobDialog(j.id); }
    catch (err) { alert(err.message); return false; }
  };
  const dlg = dialog({title: `Regenerate ${r.id}`, body: f, buttons: [{label: "Regenerate", default: true, onClick: go}, {label: "Cancel"}]});
  f.onsubmit = async e => { e.preventDefault(); if ((await go()) !== false) dlg.close(); };
}

async function framesAction(body) {
  try { const j = await api(`/api/projects/${slug}/frames/generate`, {...body, n: framesPerShot()}); await load(); openJobDialog(j.id); }
  catch (err) { alert(err.message); }
}

document.addEventListener("mousedown", e => {      // close the landmark picker on an outside click
  const pick = $("#fr-landmark");
  if (pick && !pick.contains(e.target)) { $(".pick-list", pick).hidden = true; $(".pick", pick).setAttribute("aria-expanded", "false"); }
});

// --- keys (only on the Frames tab, never while typing) ---------------------------------------
keyHandlers.push(e => {
  if (currentTab !== "frames" || !framesData || !framesData.shots.length) return false;
  if (e.key.startsWith("Arrow") && e.target.matches("input[type=radio], select, #shot-tree")) {
    if (!e.target.matches("#shot-tree")) return false;              // radios keep their arrows
  }
  const r = shotById(fr.shot), f = r && curFrame(r);
  if (e.key === "ArrowLeft") { stepFrame(-1); return true; }
  if (e.key === "ArrowRight") { stepFrame(1); return true; }
  if (e.key === "ArrowUp") { stepShot(-1); return true; }
  if (e.key === "ArrowDown") { stepShot(1); return true; }
  if (!f) return false;
  const d = frameDraft(f);
  if (e.key === "s" || e.key === "S") { if (d.role !== "rejected") { d.starred = !d.starred; d.changed = true; renderFrameRight(r, f); } return true; }
  if (["1", "2", "3"].includes(e.key)) {
    d.role = Object.keys(ROLE_LABELS)[+e.key - 1]; d.changed = true;
    if (d.role === "rejected") d.starred = false;
    renderFrameRight(r, f); return true;
  }
  if (e.key === "Enter" && !e.target.closest("button, a")) { saveAndNext(); return true; }
  return false;
});
saveHandlers.frames = () => saveDraft().then(ok => ok && load());
revertHandlers.frames = () => { fr.draft = null; fr.shown = ""; fr.rshown = ""; renderFrames(); };
