// Library › Asset review › build review: one asset's detail view (opened from its card).
// Reference view vs the current build (sheet angle and three-quarter, Cycles with its
// materials; or an orbit of its GLB) vs the previous round's; its kit brief as a form (validated
// against the catalog's ranges); Save & Rebuild (a CPU job: quick preview first, Cycles after);
// the review status and note with its history per round. Keys: ← → previous / next asset,
// A approve, C needs changes.
const lr = {asset: null, data: null, list: null, draft: null, dirty: false, orbit: false, prev: "threeq",
            pick: null, poll: null, briefs: new Set()};
// choices per key ("parent.key" first: kind means different things in a window and a ground storey);
// the server's list (building_gen.ENUMS) comes with each asset
let LR_ENUMS = {};
const lrEnum = path => {
  const keys = path.split(".").filter(k => isNaN(+k));
  return LR_ENUMS[keys.slice(-2).join(".")] || LR_ENUMS[keys.at(-1)];
};
const LR_STATUS_CLASS = {approved: "m-ok", needs_changes: "m-chg", needs_feature: "m-new", ready: "m-rdy", needs_review: "m-rdy"};
const lrClone = o => o == null ? o : JSON.parse(JSON.stringify(o));

let lrDeepLink = URL_ARGS.get("review");      // ?project=…&review=<asset>: open that asset's review (task list links)
async function loadAssetReviews() {
  if (!slug) { lr.list = null; lr.briefs = new Set(); return; }
  try { lr.list = await api(`/api/projects/${slug}/library/reviews`); lr.briefs = new Set(lr.list.assets.map(a => a.asset)); }
  catch (_) { lr.list = null; lr.briefs = new Set(); }
  if (lrDeepLink && lr.briefs.has(lrDeepLink)) {
    const a = lrDeepLink; lrDeepLink = null;
    showTab("library"); showSub("assets"); setTimeout(() => openAssetReview(a), 0);
  }
}

// the assets with a procedural brief that belong to a library entry (itself, or its kit modules)
const lrAssetsOf = id => [...lr.briefs].filter(a => a === id || a.startsWith(id + "-"));

async function openAssetReview(asset) {
  lr.asset = asset; lr.orbit = false; lr.pick = null;
  $("#assets-list").hidden = true; $("#assets-tab > .row").hidden = true;
  let box = $("#asset-detail");
  if (!box) { box = document.createElement("div"); box.id = "asset-detail"; $("#assets-tab").append(box); }
  box.hidden = false;
  await lrLoad(true);
}

function closeAssetReview() {
  lr.asset = null; clearInterval(lr.poll); lr.poll = null;
  const box = $("#asset-detail"); if (box) box.hidden = true;
  $("#assets-list").hidden = false; $("#assets-tab > .row").hidden = false;
  loadAssetReviews().then(renderLibrary);
}

async function lrLoad(resetDraft) {
  const asset = lr.asset;
  const [list, d] = await Promise.all([api(`/api/projects/${slug}/library/reviews`), api(`/api/projects/${slug}/library/asset/${asset}`)]);
  if (lr.asset !== asset) return;
  lr.list = list; lr.data = d; LR_ENUMS = d.enums || {};
  if (resetDraft || !lr.draft) {
    const b = d.brief;
    lr.draft = {dims: lrClone(b.dims), params: lrClone(b.params || {}), features: lrClone(d.features), colors: lrClone(b.colors || {}),
                patches: lrClone(b.patches || {}), sheet: b.sheet, position: b.position,
                material_types: lrClone(b.material_types || {}), material_scale: lrClone(b.material_scale || {}), surface: b.surface || "auto"};
    lr.dirty = false;
  }
  lrRender();
  lrWatch();
}

function lrWatch() {      // while its rebuild runs, reload the images every 2.5 s
  const running = (lr.data.jobs || []).length || (data && data.jobs.some(j => j.kind === "library.rebuild" && j.tag.asset === lr.asset && isActive(j)));
  if (running && !lr.poll) lr.poll = setInterval(async () => {
    const asset = lr.asset; if (!asset) return;
    const d = await api(`/api/projects/${slug}/library/asset/${asset}`).catch(() => null);
    if (!d || lr.asset !== asset) return;
    lr.data = {...lr.data, images: d.images, jobs: d.jobs, review: d.review, usage: d.usage, silhouette: d.silhouette};
    lrRenderViewer(); lrRenderReview();
    if (!d.jobs.length) { clearInterval(lr.poll); lr.poll = null; lrRenderVersions(); }
  }, 2500);
}

// --- rendering ---------------------------------------------------------------------------
function lrRender() {
  const d = lr.data, box = $("#asset-detail"), rv = d.review || {};
  const e = d.entry || {};
  box.innerHTML = `
    <div class="row"><button type="button" class="small" id="lr-back">◂ Back to Asset review</button>
      <b>${esc(d.asset)}</b><span class="hint" style="margin:0">${esc([e.tier, e.typology, e.district, d.brief.form || d.brief.kind].filter(Boolean).join(" · "))} · round ${d.round}</span>
      <span class="mark ${LR_STATUS_CLASS[rv.status] || ""}" id="lr-status-chip">${esc(d.statuses[rv.status] || "not reviewed")}</span>
      <span class="spacer" style="flex:1"></span>
      <button type="button" class="small" id="lr-prev" title="Previous asset (←)">◂ Prev</button><button type="button" class="small" id="lr-next" title="Next asset (→)">Next ▸</button></div>
    <div class="lr-split">
      <fieldset class="lr-list"><legend>Assets (round ${d.round})</legend><ul class="list" id="lr-assets"></ul></fieldset>
      <div class="lr-mid"><div class="viewer lr-viewer" id="lr-viewer"></div>
        <div class="row"><label class="check"><input type="checkbox" id="lr-orbit" ${lr.orbit ? "checked" : ""}>Orbit (3D)</label>
          <label>Previous: <select id="lr-prev-view"><option value="threeq">three-quarter</option><option value="sheet">sheet angle</option></select></label>
          <span class="hint" id="lr-job" style="margin:0"></span></div>
        <fieldset id="lr-versions"></fieldset>
        <fieldset id="lr-review"></fieldset>
        <fieldset id="lr-usage"></fieldset></div>
      <div class="lr-right" id="lr-brief"></div>
    </div>`;
  $("#lr-prev-view").value = lr.prev;
  $("#lr-back").onclick = closeAssetReview;
  $("#lr-prev").onclick = () => lrStep(-1); $("#lr-next").onclick = () => lrStep(1);
  $("#lr-orbit").onchange = ev => { lr.orbit = ev.target.checked; lrRenderViewer(); };
  $("#lr-prev-view").onchange = ev => { lr.prev = ev.target.value; lrRenderViewer(); };
  const ul = $("#lr-assets");
  for (const a of (lr.list && lr.list.assets) || []) {
    const li = document.createElement("li");
    if (a.asset === d.asset) li.className = "sel";
    li.innerHTML = `<span>${esc(a.asset)}</span><span class="mark ${LR_STATUS_CLASS[a.status] || "m-none"}">${esc(a.status ? d.statuses[a.status].toLowerCase() : "–")}</span>`;
    li.onclick = () => lrGo(a.asset);
    ul.append(li);
  }
  lrRenderViewer(); lrRenderReview(); lrRenderUsage(); lrRenderBrief();
  lrRenderVersions();
}

function lrRenderVersions() {      // the version strip (versions.js); restoring reloads the brief and images
  versionStrip("#lr-versions", lr.asset, () => lrLoad(true));
}

function lrImg(path, cap, extra = "", cls = "") {
  return `<figure class="${cls}">${path ? `<a href="${fileUrl(path)}" target="_blank" title="Open large"><img alt="${esc(cap)}" src="${fileUrl(path)}"></a>`
    : `<div class="lr-empty">not rendered yet</div>`}<figcaption><span>${esc(cap)}</span><span>${extra}</span></figcaption></figure>`;
}

function lrRenderViewer() {
  const im = lr.data.images, box = $("#lr-viewer"); if (!box) return;
  const cur = im.current || {}, prev = im.previous || {};
  const q = im.quick ? " · quick preview (Cycles running)" : "";
  const ref = lrImg(im.reference, "Reference · " + (lr.data.brief.sheet || "").split("/").pop() + (lr.data.brief.position != null ? ` · ${["left", "middle", "right"][lr.data.brief.position] || lr.data.brief.position}` : ""),
                    `frontality ${(lr.data.brief.measured || {}).frontality ?? "–"}`, "lr-ref");
  const center = lr.orbit && im.glb
    ? `<figure class="lr-orbit"><model-viewer src="${fileUrl(im.glb)}" camera-controls shadow-intensity="0.7" exposure="0.9"
         environment-image="neutral" alt="3D model of ${esc(lr.data.asset)}"></model-viewer><figcaption><span>Current build · orbit: drag to turn, scroll to zoom</span><span></span></figcaption></figure>`
    : lrImg(cur.sheet, `Current · sheet angle${q}`, im.current_round ? `round ${im.current_round}` : "") +
      lrImg(cur.threeq, `Current · three-quarter${q}`, "");
  box.classList.toggle("orbit", !!(lr.orbit && im.glb));
  box.innerHTML = ref + center + lrImg(prev[lr.prev], `Previous build${im.previous_round ? ` · round ${im.previous_round}` : ""}`, "", "prev");
  $(".lr-ref img", box)?.addEventListener("click", lrPickClick);
  const j = $("#lr-job");
  if (j) j.textContent = im.job && im.job.step < 5 ? `Rebuild: ${im.job.line} (${im.job.step} of 5)` : "";
}

function lrRenderReview() {
  const d = lr.data, rv = d.review || {}, box = $("#lr-review"); if (!box) return;
  const chip = $("#lr-status-chip");
  if (chip) { chip.className = `mark ${LR_STATUS_CLASS[rv.status] || ""}`; chip.textContent = d.statuses[rv.status] || "not reviewed"; }
  if (box.dataset.asset === d.asset && box.contains(document.activeElement)) return;     // don't clobber typing
  box.dataset.asset = d.asset;
  const opts = [["approved", "Approved (A)"], ["needs_changes", "Needs changes (C)"], ["needs_feature", "Needs new feature"]];
  box.innerHTML = `<legend>Review</legend>
    <div class="row" role="radiogroup" aria-label="Status">${opts.map(([k, l]) => `<label class="check"><input type="radio" name="lr-st" value="${k}" ${rv.status === k ? "checked" : ""}>${l}</label>`).join("")}
      ${rv.status === "ready" || rv.status === "needs_review" ? `<span class="hint" style="margin:0">now: ${esc(d.statuses[rv.status])}</span>` : ""}</div>
    <textarea id="lr-note" aria-label="Notes" placeholder="What to change, or the feature it needs">${esc(rv.note || "")}</textarea>
    <div class="row end"><span class="hint" style="margin:0;flex:1">Saved to site/library_review.json with this round's images; Library › Review round: task list shows it.</span>
      <button type="button" class="default" id="lr-save-review">Save review</button></div>
    <h3>History</h3><ul class="lr-hist">${(rv.history || []).slice().reverse().map(h => `<li><span class="mono">round ${h.round}</span>
      <span class="mark ${LR_STATUS_CLASS[h.status] || "m-none"}">${esc(d.statuses[h.status] || "cleared")}</span><span>${esc(h.note || "")} <span class="hint">· ${esc((h.at || "").slice(0, 10))}</span></span></li>`).join("") || `<li class="hint">No reviews yet.</li>`}</ul>`;
  $("#lr-save-review").onclick = () => lrSaveReview();
}

async function lrSaveReview(status) {
  const st = status || ($("input[name=lr-st]:checked") || {}).value || null;
  const note = $("#lr-note") ? $("#lr-note").value : "";
  try { lr.data.review = await api(`/api/projects/${slug}/library/asset/${lr.asset}/review`, {status: st, note}); }
  catch (err) { alert(errorText(err)); return; }
  const it = lr.list && lr.list.assets.find(a => a.asset === lr.asset); if (it) it.status = lr.data.review.status;
  $("#lr-review").dataset.asset = "";
  lrRender();
}

function lrRenderUsage() {
  const u = lr.data.usage, box = $("#lr-usage"); if (!box) return;
  const LOCK = {frame: "locked frame", greybox: "greybox locked"};
  box.innerHTML = `<legend>Where it's used</legend>
    <p style="margin:0 0 4px">${u.instances} instance(s) in the greybox${u.size_m ? ` · library size ${u.size_m.map(v => +v.toFixed(1)).join(" × ")} m` : ""}</p>
    ${u.shots.length ? `<ul class="list lr-shots">${u.shots.map(s => `<li><span>${esc(s.shot)} <span class="hint">${esc(s.tier)}</span></span>
      <span>${(100 * s.frac).toFixed(2)}% ${s.lock ? `<span class="mark ${s.lock === "frame" ? "m-chg" : "m-none"}">${LOCK[s.lock]}</span>` : ""}</span></li>`).join("")}</ul>`
      : `<p class="hint" style="margin:0">No shot shows it.</p>`}
    ${lrSilhouette()}`;
}

function lrSilhouette() {    // silhouette checks against locked frames (ap library silhouette / ap ue look)
  const sh = lr.data.silhouette || {}, ids = Object.keys(sh);
  if (!ids.length) return `<p class="hint" style="margin:4px 0 0">No silhouette check yet.</p>`;
  return `<p style="margin:6px 0 2px"><b>Silhouette vs locked frame</b></p><ul class="list lr-shots">${ids.map(id => {
    const r = sh[id], ok = r.score >= 0.85, ook = (r.openings_iou ?? 0) >= 0.7;
    return `<li><span>${esc(id)} <span class="mark ${ok ? "m-ok" : "m-chg"}">silhouette ${r.score.toFixed(2)}</span>
        ${r.openings_iou != null ? `<span class="mark ${ook ? "m-ok" : "m-chg"}">openings ${r.openings_iou.toFixed(2)}</span>` : ""}</span>
      <span class="hint" style="margin:0">IoU ${r.iou.toFixed(2)} · aspect ${r.aspect_build} / frame ${r.aspect_frame}</span></li>
      ${r.overlay ? `<li><img src="${fileUrl(r.overlay, r.at)}" alt="silhouette overlay ${esc(id)}" style="max-width:100%"></li>` : ""}
      ${r.openings_overlay ? `<li><img src="${fileUrl(r.openings_overlay, r.at)}" alt="openings overlay ${esc(id)}" style="max-width:100%"></li>` : ""}`;
  }).join("")}</ul>`;
}

// --- the brief form ------------------------------------------------------------------------
function lrSet(path, value) {
  const keys = path.split("."); let o = lr.draft;
  for (const k of keys.slice(0, -1)) { if (o[k] == null || typeof o[k] !== "object") o[k] = {}; o = o[k]; }
  o[keys.at(-1)] = value;
  if (!lr.dirty) { lr.dirty = true; const n = $("#lr-dirty"); if (n) n.hidden = false; }
  lrValidateSoon();
}
const lrGet = path => path.split(".").reduce((o, k) => o == null ? undefined : o[k], lr.draft);

function lrField(path, v, label) {
  const id = "lrf-" + path.replace(/\W/g, "_"), last = path.split(".").at(-1);
  if (typeof v === "boolean") return `<label class="check"><input type="checkbox" id="${id}" data-path="${path}" data-t="bool" ${v ? "checked" : ""}>${esc(label)}</label>`;
  let input;
  if (typeof v === "number") input = `<input type="number" id="${id}" data-path="${path}" data-t="num" value="${v}" step="any">`;
  else if (Array.isArray(v)) {
    const simple = v.every(x => typeof x === "string" || typeof x === "number");
    input = `<input id="${id}" data-path="${path}" data-t="${simple ? "list" : "json"}" value="${esc(simple ? v.join(", ") : JSON.stringify(v))}" style="width:12em">`;
  } else {
    const opts = lrEnum(path);
    input = opts ? `<select id="${id}" data-path="${path}" data-t="str">${[...new Set([v, ...opts])].map(o => `<option${o === v ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`
      : `<input id="${id}" data-path="${path}" data-t="str" value="${esc(v ?? "")}" style="width:8em">`;
  }
  return `<label class="lr-f" for="${id}"><span>${esc(label)}</span>${input}</label>`;
}

function lrPart(path, v, dflt, depth) {
  // one part: a toggle (null = off) and its fields; nested dicts as their own group
  const name = path.split(".").at(-1);
  const tmpl = (lr.data.templates || {})[name];
  const on = v != null;
  const user = JSON.stringify(v) !== JSON.stringify(dflt);
  const head = `<label class="check"><input type="checkbox" data-toggle="${path}" ${on ? "checked" : ""} ${!on && !tmpl && dflt == null ? "disabled" : ""}>${esc(name.replace(/_/g, " "))}</label>${user ? ` <span class="mark m-rdy" title="set in this brief">brief</span>` : ""}`;
  if (!on) return `<div class="lr-part off">${head}</div>`;
  const plain = [], subs = [];
  for (const [k, x] of Object.entries(v)) {
    const p = `${path}.${k}`;
    if (lrIsList(k, x)) subs.push(lrList(p, x));
    else if (x && typeof x === "object" && !Array.isArray(x)) subs.push(lrPart(p, x, dflt && dflt[k], depth + 1));
    else if (x === null && ((lr.data.templates || {})[k] || (dflt && dflt[k]))) subs.push(lrPart(p, x, dflt && dflt[k], depth + 1));
    else if (x !== null) plain.push(lrField(p, x, k.replace(/_/g, " ")));
  }
  return depth >= 1 ? `<details class="lr-part" ${user ? "open" : ""}><summary>${head}</summary><div class="lr-fields">${plain.join("")}</div>${subs.join("")}</details>`
    : `<details class="lr-part" ${user ? "open" : ""}><summary>${head}</summary><div class="lr-fields">${plain.join("")}</div>${subs.join("")}</details>`;
}

// a list of parts (annexes, recesses, house types): one group per item, Add / Remove
const lrIsList = (k, x) => Array.isArray(x) && (x.some(i => i && typeof i === "object")
  || Array.isArray((lr.data.templates || {})[k]));
function lrList(path, arr) {
  const name = path.split(".").at(-1), label = name.replace(/_/g, " ");
  const items = arr.map((x, i) => {
    const plain = [], subs = [];
    for (const [k, y] of Object.entries(x || {})) {
      const p = `${path}.${i}.${k}`;
      if (y && typeof y === "object" && !Array.isArray(y)) subs.push(lrPart(p, y, null, 1));
      else if (y !== null) plain.push(lrField(p, y, k.replace(/_/g, " ")));
    }
    return `<details class="lr-part" open><summary><span>${esc(label)} ${i + 1}</span>
      <button type="button" class="small" data-del="${path}" data-i="${i}">Remove</button></summary>
      <div class="lr-fields">${plain.join("") || `<span class="hint">no fields: Remove, then Add for the template</span>`}</div>${subs.join("")}</details>`;
  }).join("");
  return `<div class="lr-part lr-list"><div class="row"><b>${esc(label)}</b> <span class="hint" style="margin:0">${arr.length} item(s)</span>
    <button type="button" class="small" data-add="${path}">Add</button></div>${items}</div>`;
}

function lrRenderBrief() {
  const d = lr.data, b = lr.draft, box = $("#lr-brief"); if (!box) return;
  const cat = d.catalog, meas = d.brief.measured || {};
  const views = (d.sheets || []).flatMap(s => [0, 1, 2, "all"].map(p => [s, p]));
  const pos = p => p === "all" ? "whole sheet" : ["left", "middle", "right"][p];
  // the storey height is the brief's parameter (above); the type default would only repeat it
  const top = d.building ? Object.entries(b.features).filter(([k, v]) => k !== "storey_h" && !lrIsList(k, v) && !(v && typeof v === "object" && !Array.isArray(v)) && v !== null) : [];
  const parts = d.building ? Object.entries(b.features).filter(([k, v]) => lrIsList(k, v) || (v && typeof v === "object" && !Array.isArray(v)) || (v === null && (d.templates[k] || (d.defaults || {})[k]))) : [];
  const pal = (meas.palette || []).map(c => `<span class="sw" style="background:rgb(${c.rgb.join(",")})" title="${Math.round(100 * c.share)}%"></span>`).join("");
  const band = name => {
    const bx = b.patches[name] || (meas.patches || {})[name] || meas.patch_px;
    const col = b.colors[name], read = ((meas.materials || {})[name] || {}).type;
    const mt = `<select data-mtype="${name}" aria-label="${name} material type" title="${esc(((meas.materials || {})[name] || {}).why || "")}">
      <option value="">auto${read ? ` (${esc(read)})` : ""}</option>${(d.material_types || []).map(t => `<option${b.material_types[name] === t ? " selected" : ""}>${t}</option>`).join("")}</select>
      <input type="number" data-mscale="${name}" min="0.05" max="20" step="any" style="width:4.5em" value="${(b.material_scale || {})[name] ?? 1}" aria-label="${name} texture scale" title="Texture scale: a multiplier on the material's tile size (0.5 = half-size joints and veining, 2 = double). 1 = as scanned.">`;
    return `<span>${name}</span><span>${mt}</span><span class="lr-patch" data-band="${name}" title="${bx ? bx.join(", ") : "none"}"></span>
      <span class="inl"><input type="checkbox" data-auto="${name}" ${col ? "" : "checked"} title="Colour from the sheet's band">auto
        <input type="color" data-color="${name}" value="${col ? rgbHex(col) : "#cccccc"}" ${col ? "" : "disabled"}></span>
      <button type="button" class="small" data-pick="${name}" title="Click the reference image to take this band's patch">${lr.pick === name ? "Click the reference…" : "Pick on view"}</button>`;
  };
  box.innerHTML = `<fieldset><legend>Kit brief · library/kits/${esc(d.asset)}.json</legend>
    ${d.building ? `<div class="lr-form">
      <span>View</span><select id="lr-view">${views.map(([s, p]) => `<option value="${esc(s)}|${p}" ${s === b.sheet && String(p) === String(b.position) ? "selected" : ""}>${esc(s.split("/").pop())} · ${pos(p)}</option>`).join("")}</select>
      <span>Footprint</span><span class="inl"><input type="number" step="any" data-path="dims.0" data-t="num" value="${b.dims[0]}" aria-label="width"> × <input type="number" step="any" data-path="dims.1" data-t="num" value="${b.dims[1]}" aria-label="depth"> m
        <span class="hint">${cat && cat.width && cat.depth ? `catalog ${cat.width.join("–")} × ${cat.depth.join("–")}` : "no catalog range"}</span></span>
      <span>Height</span><span class="inl"><input type="number" step="any" data-path="dims.2" data-t="num" value="${b.dims[2]}" aria-label="height"> m
        <span class="hint">${d.usage.size_m ? `greybox ${d.usage.size_m[2]}` : ""}</span></span>
      <span>Storey</span><span class="inl"><input type="number" step="any" data-path="params.storey_h" data-t="num" value="${b.params.storey_h ?? ""}" placeholder="${b.features.storey_h ?? cat?.storey_m ?? 3.4}"> m
        <span class="hint">${cat && cat.storeys ? `catalog ${cat.storeys.join("–")} storeys` : ""}${meas.storeys ? ` · sheet ${meas.storeys} storeys` : ""}</span></span>
      <span>Bay rhythm</span><span class="inl"><input type="number" step="any" data-path="params.bay_m" data-t="num" value="${b.params.bay_m ?? ""}" placeholder="auto"> m
        <span class="hint">sheet: ${meas.bay_m ? meas.bay_m + " m" : "not measured"}</span></span>
      ${top.map(([k, v]) => `<span>${esc(k.replace(/_/g, " "))}</span><span>${lrField("features." + k, v, "").replace('<span></span>', "")}</span>`).join("")}
    </div>` : `<p class="hint" style="margin:0 0 4px">A kit piece (${esc(d.brief.kind)}): its measurements in metres.</p>
      <div class="lr-fields">${Object.entries(b.params || {}).map(([k, v]) => lrField("params." + k, v, k.replace(/_/g, " "))).join("")}</div>`}
    </fieldset>
    ${d.building ? `<fieldset><legend>Parts</legend>${parts.map(([k, v]) => lrIsList(k, v) ? lrList("features." + k, v) : lrPart("features." + k, v, (d.defaults || {})[k], 0)).join("")}</fieldset>
    <fieldset><legend>Materials per storey band</legend><div class="lr-mats"><b>Band</b><b>Type · texture scale ×</b><b>Patch</b><b>Colour</b><b></b>
      ${band("top")}${band("wall")}${band("base")}</div>
      <p class="hint" style="margin:4px 0 0">Sheet palette ${pal} · Surfaces: <select id="lr-surface" aria-label="Surfaces">
        ${["auto", "library", "patch"].map(o => `<option${(b.surface || "auto") === o ? " selected" : ""}>${o}</option>`).join("")}</select>
        (library: scanned PBR set per type, tinted; patch: a tile cut from the sheet)</p></fieldset>` : ""}
    <div class="row end"><span id="lr-dirty" class="hint" style="margin:0;flex:1" ${lr.dirty ? "" : "hidden"}>${(d.review || {}).status === "approved" ? "Unsaved edits · saving sets this Approved asset back to Needs review" : "Unsaved edits"}</span>
      <span id="lr-errors" class="err"></span><span id="lr-warnings" class="hint" style="margin:0"></span><button type="button" id="lr-revert">Revert</button><button type="button" id="lr-guide" title="Describe what should change; the vision model proposes brief edits">Guidance…</button><button type="button" class="default" id="lr-rebuild">Save &amp; Rebuild</button></div>`;
  box.querySelectorAll("[data-path]").forEach(el => el.addEventListener("change", lrFieldChange));
  box.querySelectorAll("[data-toggle]").forEach(el => el.addEventListener("change", ev => {
    const p = ev.target.dataset.toggle, name = p.split(".").at(-1);
    const dflt = p.split(".").slice(1).reduce((o, k) => o == null ? undefined : o[k], d.defaults);
    lrSet(p, ev.target.checked ? lrClone(dflt || d.templates[name] || {}) : null);
    lrRenderBrief();
  }));
  box.querySelectorAll("[data-add]").forEach(el => el.addEventListener("click", ev => {
    ev.preventDefault();
    const p = el.dataset.add, name = p.split(".").at(-1);
    const tmpl = ((d.templates || {})[name] || [{}])[0] || {};
    lrSet(p, [...(lrGet(p) || []), lrClone(tmpl)]); lrRenderBrief();
  }));
  box.querySelectorAll("[data-del]").forEach(el => el.addEventListener("click", ev => {
    ev.preventDefault();
    const p = el.dataset.del, arr = (lrGet(p) || []).slice(); arr.splice(+el.dataset.i, 1);
    lrSet(p, arr); lrRenderBrief();
  }));
  box.querySelectorAll("[data-auto]").forEach(el => el.addEventListener("change", ev => {
    const n = ev.target.dataset.auto;
    if (ev.target.checked) delete lr.draft.colors[n]; else lr.draft.colors[n] = hexRgb($(`[data-color=${n}]`, box).value);
    lrSet("colors", lr.draft.colors); lrRenderBrief();
  }));
  box.querySelectorAll("[data-color]").forEach(el => el.addEventListener("input", ev => {
    lr.draft.colors[ev.target.dataset.color] = hexRgb(ev.target.value); lrSet("colors", lr.draft.colors);
  }));
  box.querySelectorAll("[data-mtype]").forEach(el => el.addEventListener("change", () => {
    const n = el.dataset.mtype;
    if (el.value) lr.draft.material_types[n] = el.value; else delete lr.draft.material_types[n];
    lrSet("material_types", lr.draft.material_types);
  }));
  box.querySelectorAll("[data-mscale]").forEach(el => el.addEventListener("change", () => {
    const n = el.dataset.mscale, x = parseFloat(el.value);
    if (x === 1 || !el.value) delete lr.draft.material_scale[n]; else lr.draft.material_scale[n] = x;
    lrSet("material_scale", lr.draft.material_scale);
  }));
  const sf = $("#lr-surface"); if (sf) sf.onchange = () => lrSet("surface", sf.value);
  box.querySelectorAll("[data-pick]").forEach(el => el.onclick = () => { lr.pick = lr.pick === el.dataset.pick ? null : el.dataset.pick; lrRenderBrief(); });
  const vs = $("#lr-view"); if (vs) vs.onchange = () => { const [s, p] = vs.value.split("|"); lr.draft.sheet = s; lr.draft.position = p === "all" ? "all" : +p; lrSet("sheet", s); };
  $("#lr-revert").onclick = () => { lrLoad(true); };
  $("#lr-rebuild").onclick = lrRebuild;
  $("#lr-guide").onclick = lrGuide;
  lrPatchThumbs();
}

function lrPatchThumbs() {      // each band's patch, cut from the reference view
  const ref = lr.data.images.reference; if (!ref) return;
  const meas = lr.data.brief.measured || {};
  const img = new Image();
  img.onload = () => document.querySelectorAll("#lr-brief .lr-patch").forEach(el => {
    const n = el.dataset.band, bx = lr.draft.patches[n] || (meas.patches || {})[n] || meas.patch_px; if (!bx) return;
    const c = document.createElement("canvas"); c.width = c.height = 30;
    c.getContext("2d").drawImage(img, bx[0], bx[1], bx[2] - bx[0], bx[3] - bx[1], 0, 0, 30, 30);
    el.replaceChildren(c);
  });
  img.src = fileUrl(ref);
}

function lrPickClick(ev) {
  if (!lr.pick) return;
  ev.preventDefault();
  const im = ev.target, r = im.getBoundingClientRect();
  const sx = im.naturalWidth / r.width, sy = im.naturalHeight / r.height;
  const x = Math.round((ev.clientX - r.left) * sx), y = Math.round((ev.clientY - r.top) * sy), h = 20;
  lr.draft.patches[lr.pick] = [Math.max(0, x - h), Math.max(0, y - h), x + h, y + h];
  lr.pick = null; lrSet("patches", lr.draft.patches); lrRenderBrief();
}

function lrFieldChange(ev) {
  const el = ev.target, t = el.dataset.t, p = el.dataset.path;
  let v;
  if (t === "bool") v = el.checked;
  else if (t === "num") v = el.value === "" ? null : +el.value;
  else if (t === "list") v = el.value.split(",").map(s => s.trim()).filter(Boolean).map(s => isNaN(+s) ? s : +s);
  else if (t === "json") { try { v = JSON.parse(el.value); el.classList.remove("bad"); } catch (_) { el.classList.add("bad"); return; } }
  else v = el.value;
  if (p.startsWith("params.") && v === null && lr.data.building) { delete lr.draft.params[p.slice(7)]; lrSet("params", lr.draft.params); return; }
  lrSet(p, v);
}

let lrValTimer = null;
function lrValidateSoon() { clearTimeout(lrValTimer); lrValTimer = setTimeout(lrValidate, 350); }
async function lrValidate() {
  if (!lr.asset || !lr.data.building) return null;
  const v = await api(`/api/projects/${slug}/library/asset/${lr.asset}/check`, {brief: lrBriefBody()}).catch(err => ({errors: [errorText(err)], warnings: [], impact: []}));
  const e = $("#lr-errors"); if (e) e.textContent = v.errors.length ? v.errors.join(" · ") : "";
  const w = $("#lr-warnings"); if (w) w.textContent = v.warnings.length ? "Note: " + v.warnings.join(" · ") : "";
  const btn = $("#lr-rebuild"); if (btn) { btn.disabled = !!v.errors.length; btn.title = v.errors.length ? "Fix the highlighted values first" : ""; }
  return v;
}

const lrBriefBody = () => ({dims: lr.draft.dims, params: lr.draft.params, features: lr.draft.features, colors: lr.draft.colors,
                            patches: lr.draft.patches, sheet: lr.draft.sheet, position: lr.draft.position,
                            material_types: lr.draft.material_types, material_scale: lr.draft.material_scale, surface: lr.draft.surface});

async function lrRebuild() {
  const v = lr.data.building ? await lrValidate() : {errors: [], impact: []};
  if (v && v.errors.length) return;
  const go = async confirm => {
    try { await api(`/api/projects/${slug}/library/asset/${lr.asset}/rebuild`, {brief: lr.data.building ? lrBriefBody() : {params: lr.draft.params}, confirm}); }
    catch (err) { alert(errorText(err)); return; }
    lr.dirty = false;
    await lrLoad(true);
    lr.data.jobs = [{}]; lrWatch();
  };
  if (v && v.impact && v.impact.length) {
    const body = document.createElement("div");
    body.innerHTML = `<p>This changes the asset's ${[...new Set(v.impact.flatMap(r => r.changed))].map(k => ({w: "width", d: "depth", h: "height"})[k]).join(" and ")}.
      These locked shot frames show its instances, so the build will no longer match the greybox they were painted on
      (a layout change to match would alter their greybox):</p>
      <ul>${v.impact.map(r => `<li><b>${esc(r.shot)}</b>: ${(100 * r.frac).toFixed(2)}% of the frame · ${esc(r.frame || "")}</li>`).join("")}</ul>
      <p>Rebuild anyway?</p>`;
    dialog({title: "Locked frames show this asset", body, buttons: [{label: "Rebuild anyway", default: true, onClick: () => go(true)}, {label: "Cancel"}]});
    return;
  }
  go(false);
}

function lrGuide() {            // guidance for the next build (refs.js guideRun), from the saved brief's sheet
  const sheet = lr.data.brief.sheet;
  const body = document.createElement("div");
  body.innerHTML = `<p>Say what the next build should change. ${lr.dirty ? "Unsaved edits in the form are not part of this: save them first. " : ""}</p>
    <textarea id="lr-guidance" rows="4" style="width:100%" aria-label="Guidance for the next build"></textarea>`;
  $("#lr-guidance", body).value = lr.data.brief.guidance || "";
  if (!sheet) { body.insertAdjacentHTML("afterbegin", `<p class="error">This brief has no reference sheet to read.</p>`); }
  dialog({title: "Guidance for the next build", body, buttons: [{label: "Propose changes", default: true, onClick: () => {
    const g = $("#lr-guidance", body).value.trim();
    if (!g || !sheet) return false;
    guideRun(lr.asset, sheet, "front", g, async () => { lr.dirty = false; await lrLoad(true); lr.data.jobs = [{}]; lrWatch(); });
  }}, {label: "Cancel"}]});
}

// --- navigation, keys ------------------------------------------------------------------------
function lrGo(asset) {
  if (lr.dirty && !window.confirmDiscard) {
    const body = document.createElement("div"); body.innerHTML = `<p>The brief of <b>${esc(lr.asset)}</b> has unsaved edits.</p>`;
    dialog({title: "Discard edits?", body, buttons: [{label: "Discard", default: true, onClick: () => { lr.dirty = false; openAssetReview(asset); }}, {label: "Cancel"}]});
    return;
  }
  openAssetReview(asset);
}
function lrStep(k) {
  const list = (lr.list && lr.list.assets) || []; if (!list.length) return;
  const i = list.findIndex(a => a.asset === lr.asset);
  lrGo(list[(i + k + list.length) % list.length].asset);
}

keyHandlers.push(e => {
  if (currentTab !== "library" || !lr.asset || $("#asset-detail")?.hidden) return false;
  if (e.key === "ArrowLeft") { lrStep(-1); return true; }
  if (e.key === "ArrowRight") { lrStep(1); return true; }
  if (e.key === "a" || e.key === "A") { lrSaveReview("approved"); return true; }
  if (e.key === "c" || e.key === "C") { lrSaveReview("needs_changes"); return true; }
  return false;
});

function rgbHex(c) { return "#" + c.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join(""); }
function hexRgb(h) { return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); }
