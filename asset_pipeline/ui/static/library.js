// Library › Asset review (world mode phase 3): one card per library entry, with its crop
// from the best current shot frame (object-id pass), the automatic flags (SAM 3.1 check)
// and the user's call: good source / use description / exclude. Decisions are saved per
// entry on the server (site/library_review.json) and survive re-planning.
let libraryData = null;
const lib = {filter: null, slug: null};
// the area the artist works on: the one last picked in Library › Areas…, else the first defined
function currentArea() {
  const names = Object.keys((libraryData && libraryData.areas) || {});
  let a = null;
  try { a = localStorage.getItem(`ap.area.${slug}`); } catch (_) { /* ignore */ }
  return names.includes(a) ? a : names[0] || null;
}
// Show filter: the choice remembered per project, else the current area, else everything
function libFilters() {
  const out = {...LIB_FILTERS};
  for (const [name, ids] of Object.entries((libraryData && libraryData.areas) || {})) {
    const set = new Set(ids);
    out[`area:${name}`] = [`Area: ${name}`, e => set.has(e.id)];
  }
  return out;
}
function libFilterNow(filters) {
  if (lib.slug !== slug) {
    lib.slug = slug; lib.filter = null;
    try { lib.filter = localStorage.getItem(`ap.libFilter.${slug}`); } catch (_) { /* ignore */ }
  }
  if (!lib.filter || !filters[lib.filter]) lib.filter = currentArea() ? `area:${currentArea()}` : "all";
  return lib.filter;
}
const LIB_DECISIONS = [["source", "Frame crop"], ["description", "Description only"],
  ["reference", "Description + chosen image"], ["exclude", "Exclude"], ["kit", "Facade kit"]];
const LIB_TIERS = {A: "landmark / kit monument", B: "singular civic", C: "fill typology", D: "prop / vegetation"};

async function loadLibrary() {
  if (!slug) { libraryData = null; return; }
  try { libraryData = await api(`/api/projects/${slug}/library`); } catch (_) { libraryData = null; }
  if (typeof loadAssetReviews === "function") await loadAssetReviews();
}

function libEntries() {
  const p = libraryData && libraryData.plan;
  return p ? [...p.entries, ...p.unseen.map(u => ({...u, best: null, sources: [], unseen: true}))] : [];
}
// a library asset's label: its entry's current name (sw_library.asset_names), else the fallback / id
function assetName(id, fallback) { return (libraryData && libraryData.names && libraryData.names[id]) || fallback || id; }
// the name, with the id as a hint when they differ (HTML)
function assetLabelHtml(id) {
  const n = assetName(id);
  return n === id ? esc(id) : `${esc(n)} <span class="hint" style="margin:0">${esc(id)}</span>`;
}
// the entry an asset belongs to: itself, or the kit decision it is a module of
function assetEntry(id) {
  const ids = libEntries().map(e => e.id);
  if (ids.includes(id)) return id;
  return ids.filter(e => id.startsWith(e + "-") && ((libraryData.decisions[e] || {}).pieces || {})[id.slice(e.length + 1)])
    .sort((a, b) => b.length - a.length)[0] || null;
}
const LIB_ROUTES = {procedural: "procedural", trellis: "TRELLIS"};
const libRoute = e => (libraryData.decisions[e.id] || {}).route || e.route_auto || null;
const libDecision = e => (libraryData.decisions[e.id] || {}).decision || null;
const LIB_FILTERS = {
  all: ["All assets", () => true], todo: ["Not decided", e => !libDecision(e)],
  flagged: ["Flagged crops", e => !e.unseen && e.flags.some(f => !f.startsWith("approximate"))],
  seen: ["With a crop", e => !e.unseen], unseen: ["Without a crop", e => e.unseen], source: ["Frame crop", e => libDecision(e) === "source"],
  description: ["Description only", e => libDecision(e) === "description"],
  reference: ["Description + chosen image", e => libDecision(e) === "reference"],
  exclude: ["Excluded", e => libDecision(e) === "exclude"]};
const shortFrame = k => k.split("/").slice(1).join(" / ").replace(/batch_0*/, "b").replace(/frame_0*(\d)/, "f$1").replace(/\.png$/, "");

function libCard(e) {
  const el = document.createElement("div");
  const d = libDecision(e);
  el.className = `asset-card${d ? ` decided-${d}` : ""}`;
  el.dataset.entry = e.id;
  const src = e.best ? shortFrame(e.best) : "";
  const ref = d === "reference" ? libraryData.decisions[e.id].frame : null;
  const foliage = e.kind === "vegetation";
  const flags = e.flags.map(f => `<li class="${f.startsWith("approximate") ? "note" : "flag"}">${esc(f)}</li>`).join("");
  el.innerHTML = `${e.best ? `<a href="${fileUrl(e.best)}" target="_blank" title="Open the source frame">
        <img loading="lazy" alt="" src="${fileUrl(`site/library/crops/${e.id}.jpg`)}"></a>`
      : `<div class="no-crop">no current shot frame shows it</div>`}
    <div class="asset-body">
      <div class="asset-name"><a href="#" class="jump-link" data-jump="refs" data-asset="${esc(e.id)}" title="Show its reference sheets"><b>${esc((libraryData.decisions[e.id] || {}).name || e.name || e.id)}</b></a>${((libraryData.decisions[e.id] || {}).name || e.name) ? ` <span class="hint" style="margin:0">${esc(e.id)}</span>` : ""} <span class="chip tier-${e.tier}" title="${esc(LIB_TIERS[e.tier])}">${e.tier}</span>
        ${libRoute(e) ? `<button type="button" class="chip route route-${libRoute(e)}" data-route="${libRoute(e)}"
          title="Build method${(libraryData.decisions[e.id] || {}).route ? " (yours)" : " (default)"}: click to switch">${LIB_ROUTES[libRoute(e)]}</button>` : ""}</div>
      <div class="hint" style="margin:0">${esc(e.part !== e.typology ? `${e.part} of ${e.typology}` : e.typology)}${e.material ? ` · ${esc(e.material)}` : ""}
        · ${e.instances ? `${e.instances}× placed` : "not placed"}${e.best ? ` · ${(100 * e.share).toFixed(1)}% of ${esc(src)}` : ""}</div>
      ${flags ? `<ul class="asset-flags">${flags}</ul>` : `<div class="asset-ok">no flags</div>`}
      ${ref ? `<a class="asset-ref" href="${fileUrl(ref)}" target="_blank" title="Look reference for its sheets">
        <img alt="" src="${fileUrl(ref)}"><span>reference: ${esc(shortFrame(ref))}</span></a>` : ""}
      ${typeof lrAssetsOf === "function" && lrAssetsOf(e.id).length ? `<div class="asset-builds">${lrAssetsOf(e.id).map(a => {
          const st = ((lr.list && lr.list.assets.find(x => x.asset === a)) || {}).status;
          return `<button type="button" class="small" data-review="${esc(a)}" title="Build review: reference vs build, brief, rebuild">Build review${lrAssetsOf(e.id).length > 1 ? `: ${esc(a.slice(e.id.length + 1))}` : ""}</button>${st ? ` <span class="mark ${LR_STATUS_CLASS[st]}">${esc(lr.list.statuses[st])}</span>` : ""}`;
        }).join(" ")}</div>` : ""}
      <div class="asset-manage"><button type="button" class="small" data-manage="${esc(e.id)}"
        title="Name, description, build method and decision; duplicate or rename">Manage entry…</button>${e.custom ? ` <span class="chip" title="Made in Library › Entries: a re-plan keeps it">yours</span>` : ""}</div>
      <div class="asset-actions" role="group" aria-label="Decision for ${esc(e.id)}">
        ${LIB_DECISIONS.filter(([k]) => k !== "kit" || d === "kit").map(([k, label]) => `<button type="button" data-decision="${k}" aria-pressed="${d === k}"
          title="${d === k && (libraryData.decisions[e.id] || {}).auto ? "Set automatically: no usable crop" : ""}"
          ${foliage ? "disabled title=\"Unreal foliage: not generated\"" : k === "source" && !e.best ? "disabled title=\"No crop to use\"" : ""}>${label}${d === k && (libraryData.decisions[e.id] || {}).auto ? " (auto)" : ""}</button>`).join("")}
      </div>
    </div>`;
  return el;
}

function renderLibrary() {
  const box = $("#assets-list"); if (!box) return;
  const head = $("#assets-summary");
  const planJobs = activeJobs("library.plan"), checkJobs = activeJobs("library.check");
  $("#lib-plan").disabled = !!planJobs.length || !(libraryData && libraryData.has_greybox);
  $("#lib-plan").textContent = planJobs.length ? "Discovering assets…" : "Discover assets from locked shots";
  $("#lib-check").disabled = !!checkJobs.length || !(libraryData && libraryData.plan);
  $("#lib-check").textContent = checkJobs.length ? "Checking…" : "Check crops (SAM)";
  if (!libraryData || !libraryData.plan) {
    head.textContent = libraryData && libraryData.has_greybox ? "No library plan yet: Discover assets from locked shots builds it from the locked shot frames." :
      "World mode only: build the greybox and lock shot frames first.";
    box.replaceChildren(); return;
  }
  const all = libEntries();
  const nd = all.filter(e => libDecision(e)).length;
  const p = libraryData.plan, out = (p.candidates || []).filter(c => !c.ok);
  head.innerHTML = `${all.length} assets · ${nd} decided · ${all.filter(LIB_FILTERS.flagged[1]).length} flagged ·
    sources: ${Object.values(p.sources).flat().length} current shot frame(s)${out.length ? `, ${out.length} excluded
    <span class="hint" title="${esc(out.map(c => `${c.frame}: ${c.reason}`).join("\n"))}">(old layout or off the greybox: hover)</span>` : ""}
    ${p.checked ? "" : " · <b>not checked yet</b>: run Check crops for the trees / sky flags"}`;
  const f = $("#lib-filter"), filters = libFilters(), cur = libFilterNow(filters);
  f.innerHTML = Object.entries(filters).map(([k, [label, fn]]) =>
    `<option value="${k}"${k === cur ? " selected" : ""}>${esc(label)} (${all.filter(fn).length})</option>`).join("");
  const shown = all.filter(filters[cur][1]);
  // one grid per district: assets with a crop first, then by tier and typology
  const byD = {};
  for (const e of shown) (byD[e.district || "-"] ||= []).push(e);
  const groups = Object.keys(byD).sort().map(d => ({key: d, items: byD[d].sort((a, b) => (!!a.unseen - !!b.unseen) ||
    a.tier.localeCompare(b.tier) || a.typology.localeCompare(b.typology) || (b.share || 0) - (a.share || 0))}));
  // keyed: a card is rebuilt only when what it shows changed (keyed.js), so crops never reload
  keyedSections(box, groups, {
    cls: "asset-district", rowsCls: "asset-grid",
    heading: g => `<h3>${esc(g.key)} <span class="hint" style="margin:0">${g.items.length} · ${g.items.filter(e => !e.unseen).length} with a crop</span></h3>`,
    key: e => e.id,
    sig: e => JSON.stringify([e, libraryData.decisions[e.id], assetName(e.id), fileVersion,
      (typeof lrAssetsOf === "function" && lr.briefs ? lrAssetsOf(e.id) : []).map(a => ((lr.list && lr.list.assets.find(x => x.asset === a)) || {}).status)]),
    build: libCard,
  });
  if (!shown.length) box.innerHTML = `<p class="hint">No assets match.</p>`;
}

async function libDecide(id, decision, frame = null) {
  const cur = (libraryData.decisions[id] || {}).decision || null;
  const next = cur === decision && !frame ? null : decision;          // pressing the chosen button again clears it
  try { libraryData.decisions = await api(`/api/projects/${slug}/library/decision`, {entry: id, decision: next, frame}); }
  catch (err) { alert(errorText(err)); return; }
  renderLibrary();
}

// any frame of any shot (old layouts too) as an asset's look reference
function pickReference(e) {
  const shots = (framesData && framesData.shots || []).filter(r => r.batches.some(b => b.frames.length));
  if (!shots.length) { alert("No frames in this project."); return; }
  const cur = (libraryData.decisions[e.id] || {}).frame || null;
  let shot = cur ? cur.split("/")[1] : (shots.find(r => r.id === `med-${e.district}`) || shots[0]).id, pick = cur;
  const body = document.createElement("div"); body.className = "ref-picker";
  const draw = () => {
    const r = shots.find(x => x.id === shot);
    body.innerHTML = `<label>Shot: <select>${shots.map(x => `<option${x.id === shot ? " selected" : ""}>${esc(x.id)}</option>`).join("")}</select></label>
      <p class="hint" style="margin:4px 0">The frame's look guides the sheets of <b>${esc(e.id)}</b>; its layout doesn't matter, so older-layout frames work too.</p>
      <div class="ref-batches">${[...r.batches].reverse().map(b => `<div class="ref-batch"><div class="hint" style="margin:0">${esc(b.dir.split("/").pop())}${b.archived ? " (older layout)" : ""}</div>
        <div class="ref-thumbs">${b.frames.map(f => `<button type="button" class="thumb${f.key === pick ? " sel" : ""}" data-key="${esc(f.key)}" aria-pressed="${f.key === pick}">
          <img loading="lazy" alt="" src="${fileUrl(f.key)}"><span>${esc(f.key.split("/").pop().replace(".png", ""))}${data.stars[f.key] ? " ★" : ""}${r.locked === f.key ? " · locked" : ""}</span></button>`).join("")}</div></div>`).join("")}</div>`;
    $("select", body).onchange = ev => { shot = ev.target.value; draw(); };
    body.querySelectorAll("[data-key]").forEach(t => t.onclick = () => { pick = t.dataset.key; draw(); });
  };
  draw();
  dialog({title: `Reference frame for ${e.id}`, body, className: "wide", buttons: [
    {label: "Use this frame", default: true, onClick: async () => { if (!pick) { alert("Pick a frame first."); return false; }
      await libDecide(e.id, "reference", pick); }},
    {label: "Cancel"}]});
}

$("#assets-list").addEventListener("click", async e => {
  const mb = e.target.closest("[data-manage]");
  if (mb) { manageEntry(mb.dataset.manage); return; }
  const rb = e.target.closest("[data-review]");
  if (rb) { openAssetReview(rb.dataset.review); return; }
  const r = e.target.closest("[data-route]");
  if (r) {
    const id = r.closest("[data-entry]").dataset.entry, ent = libEntries().find(x => x.id === id);
    const next = r.dataset.route === "procedural" ? "trellis" : "procedural";
    try { libraryData.decisions = await api(`/api/projects/${slug}/library/route`, {entry: id, route: next === ent.route_auto ? null : next}); }
    catch (err) { alert(errorText(err)); return; }
    renderLibrary(); return;
  }
  const b = e.target.closest("[data-decision]"); if (!b) return;
  const id = b.closest("[data-entry]").dataset.entry;
  if (b.dataset.decision === "reference" && libDecision({id}) !== "reference") {
    pickReference(libEntries().find(x => x.id === id)); return;
  }
  libDecide(id, b.dataset.decision);
});
$("#lib-filter").onchange = e => {
  lib.filter = e.target.value;
  try { localStorage.setItem(`ap.libFilter.${slug}`, lib.filter); } catch (_) { /* ignore */ }
  renderLibrary();
};
// Library asset plan from the locked shots' frames (CPU, seconds); opens Library › Asset review
async function discoverAssets() {
  try { await api(`/api/projects/${slug}/library/plan`, {}); } catch (err) { alert(errorText(err)); return; }
  showTab("library"); showSub("assets");
  await load();
}
$("#lib-plan").onclick = () => discoverAssets();
Object.assign(ACTIONS, {discoverAssets: {run: () => discoverAssets(), enabled: () => !!slug && !!siteData && !!siteData.summary && !activeJobs("library.plan").length}});
$("#lib-check").onclick = async () => { try { await api(`/api/projects/${slug}/library/check`, {}); } catch (err) { alert(errorText(err)); } };

// --- Entries management: create, duplicate, rename, describe, route, decide ------------------
const entryField = (id, label, input) => `<label for="${id}">${label}</label>${input}`;

async function entriesReload() { await load(); }

// an in-page yes/no (native confirm() can't be driven or seen by the page tests)
function askDialog(title, text, ok = "OK") {
  return new Promise(res => {
    let answered = false;
    const body = document.createElement("p"); body.textContent = text;
    dialog({title, body, onClose: () => { if (!answered) res(false); }, buttons: [
      {label: ok, default: true, onClick: () => { answered = true; res(true); }}, {label: "Cancel"}]});
  });
}

// run an entries call; "files already exist" asks before overwriting, then retries
async function entriesCall(path, body) {
  try { return await api(`/api/projects/${slug}/library/entries${path}`, body); }
  catch (err) {
    const msg = errorText(err);
    if (/^files already exist/.test(msg) && !body.overwrite && await askDialog("Overwrite?", `${msg}\n\nOverwrite them?`, "Overwrite"))
      return entriesCall(path, {...body, overwrite: true});
    alert(msg); return null;
  }
}

function newEntryDialog() {
  const f = document.createElement("form"); f.className = "form"; f.id = "entry-new-form";
  f.innerHTML = `${entryField("en-id", "Id:", `<input id="en-id" name="id" placeholder="stone-counter" required pattern="[a-z0-9](?:[a-z0-9]|-)*">`)}
    ${entryField("en-name", "Name:", `<input id="en-name" name="name" placeholder="Stone counter">`)}
    ${entryField("en-desc", "Description:", `<textarea id="en-desc" name="description" rows="3" placeholder="What the sheets should show"></textarea>`)}
    ${entryField("en-kind", "Kind:", `<select id="en-kind" name="kind"><option value="prop">prop</option><option value="building">building</option></select>`)}
    ${entryField("en-route", "Build method:", `<select id="en-route" name="route"><option value="">default for the kind (props TRELLIS, buildings procedural)</option>
      <option value="procedural">procedural</option><option value="trellis">TRELLIS</option></select>`)}
    ${entryField("en-size", "Size (m):", `<input id="en-size" name="size" placeholder="w d h, e.g. 3 1 1 (optional)">`)}
    ${entryField("en-tier", "Tier:", `<select id="en-tier" name="tier"><option value="">default (prop D, building B)</option>${Object.keys(LIB_TIERS).map(t => `<option>${t}</option>`).join("")}</select>`)}`;
  dialog({title: "New library entry", body: f, buttons: [
    {label: "Create", default: true, onClick: async () => {
      if (!f.reportValidity()) return false;
      const fd = Object.fromEntries(new FormData(f));
      const size = (fd.size || "").split(/[\s,x×]+/).filter(Boolean).map(Number);
      if (size.some(x => !(x > 0)) || ![0, 1, 3].includes(size.length)) { alert("Size: one number or width depth height, in metres."); return false; }
      const r = await entriesCall("", {id: fd.id, name: fd.name || null, description: fd.description || null, kind: fd.kind,
        route: fd.route || null, tier: fd.tier || null, size_m: size.length ? (size.length === 1 ? size[0] : size) : null});
      if (!r) return false;
      await entriesReload();
    }},
    {label: "Cancel"}]});
}

function idPrompt({title, intro, label, value, button, onGo}) {
  const f = document.createElement("form"); f.className = "form"; f.id = "entry-id-form";
  f.innerHTML = `<p class="hint" style="grid-column:1/-1">${intro}</p>${entryField("en-newid", label, `<input id="en-newid" value="${esc(value)}" required pattern="[a-z0-9](?:[a-z0-9]|-)*">`)}
    ${entryField("en-newname", "Name:", `<input id="en-newname" placeholder="optional">`)}`;
  return dialog({title, body: f, buttons: [
    {label: button, default: true, onClick: async () => {
      if (!f.reportValidity()) return false;
      return (await onGo($("#en-newid", f).value.trim(), $("#en-newname", f).value.trim())) ? undefined : false;
    }},
    {label: "Cancel"}]});
}

// --- building form and size (Brief & build and Manage entry…) ------------------------------
let FORMS_CACHE = null;
async function buildingForms() {
  if (!FORMS_CACHE) FORMS_CACHE = await api("/api/forms", undefined, "GET");
  return FORMS_CACHE;
}
// Form dropdown (each generator with a line) and Size (w × d × h m), pre-filled from info
// (library_entries.form_info). allowAuto: an "as now" first option (Manage entry…).
async function formFieldsHtml(info, {allowAuto = false} = {}) {
  const forms = await buildingForms(), cur = info.form || "", sel = cur || info.suggested;
  const src = {yours: "your choice", brief: "from its brief", entry: "the entry's", catalog: "from the catalog"}[info.source] || "";
  const opt = f => `<option value="${f.id}"${f.id === sel && !(allowAuto && !info.source) ? " selected" : ""} title="${esc(f.desc)}">${esc(f.label)}${f.id === info.suggested && !cur ? " (suggested)" : ""}</option>`;
  const size = info.size_m || ["", "", ""];
  return `${entryField("ff-form", "Form:", `<select id="ff-form">${allowAuto ? `<option value=""${info.source && info.source !== "yours" ? " selected" : ""}>${cur ? `as now (${esc((forms.find(f => f.id === cur) || {label: cur}).label)}, ${src})` : "none yet"}</option>` : ""}${forms.map(opt).join("")}</select>`)}
    <span></span><span class="hint" id="ff-form-desc" style="margin:0"></span>
    ${entryField("ff-w", "Size (m):", `<span class="ff-size"><input id="ff-w" type="number" min="0.1" step="0.01" value="${size[0]}" aria-label="Width (m)"> ×
      <input id="ff-d" type="number" min="0.1" step="0.01" value="${size[1]}" aria-label="Depth (m)"> ×
      <input id="ff-h" type="number" min="0.1" step="0.01" value="${size[2]}" aria-label="Height (m)"></span>`)}
    <span></span><span class="hint" style="margin:0">width × depth × height${info.size_source ? ` (${{yours: "your size", "kit piece": "the kit piece's size"}[info.size_source] || "from the plan"})` : ", needed for the brief"}</span>`;
}
function formFieldsWire(body) {
  const sel = $("#ff-form", body), desc = $("#ff-form-desc", body);
  const show = () => { const f = (FORMS_CACHE || []).find(x => x.id === sel.value); desc.textContent = f ? f.desc : ""; };
  sel.onchange = show; show();
}
// {form, dims} from the fields; dims null when untouched-empty; an Error for half-filled sizes
function formFieldsRead(body) {
  const v = ["#ff-w", "#ff-d", "#ff-h"].map(s => $(s, body).value.trim());
  if (v.every(x => x === "")) return {form: $("#ff-form", body).value || null, dims: null};
  const dims = v.map(Number);
  if (dims.some(x => !(x > 0))) throw new Error("Size: fill in width, depth and height, all above 0 m.");
  return {form: $("#ff-form", body).value || null, dims};
}

// Manage entry… › Variant set (prop entries): the entries a prop type's places pick from, with
// weights (variants.py). Places pick reproducibly per slot; adding an entry moves only some places
// to it. Edits are saved with the dialog's Save.
async function variantSection(id, box) {
  let info, areaNames = [];
  try {
    info = await api(`/api/projects/${slug}/library/variants/${encodeURIComponent(id)}`, undefined, "GET");
    areaNames = Object.keys((await api(`/api/projects/${slug}/areas`, undefined, "GET")).areas || {});
  } catch (err) { box.innerHTML = `<p class="error">${esc(errorText(err))}</p>`; return null; }
  let draft = info.set ? {members: Object.fromEntries(info.set.members.map(m => [m.entry, m.weight])), areas: [...info.set.areas]} : null;
  let dirty = false;
  const render = () => {
    if (!draft) {
      box.innerHTML = info.can_base ? `<fieldset><legend>Variant set</legend><p class="hint" style="margin-top:0">One entry for every ${esc(id)} place.
        A variant set lets the places pick from several entries (e.g. different stall types), by weight.</p>
        <button type="button" class="small" id="vs-make">Make a variant set</button></fieldset>`
        : `<p class="hint">Variant set: none. Add this entry from its prop type's entry (Manage entry… › Variant set there).</p>`;
      const mk = $("#vs-make", box);
      if (mk) mk.onclick = () => { draft = {members: {[id]: 1}, areas: []}; dirty = true; render(); };
      return;
    }
    const base = info.set ? info.set.base : id, total = Object.values(draft.members).reduce((a, b) => a + b, 0) || 1;
    const counts = Object.fromEntries((info.set ? info.set.members : []).map(m => [m.entry, m.slots]));
    const free = info.candidates.filter(c => !(c in draft.members));
    box.innerHTML = `<fieldset id="vs-box"><legend>Variant set of ${esc(base)}${info.set ? ` · ${info.set.slots} places (${esc(info.set.prop)})` : ""}</legend>
      <table class="grid-table" id="vs-table"><thead><tr><th>Entry</th><th>Weight</th><th>Share</th><th>Places</th><th></th></tr></thead><tbody>
      ${Object.entries(draft.members).map(([m, w]) => `<tr><td>${esc(m)}</td>
        <td><input type="number" min="0" step="0.1" value="${w}" data-vs-w="${esc(m)}" style="width:5em" aria-label="Weight of ${esc(m)}"></td>
        <td>${(100 * w / total).toFixed(0)}%</td><td>${dirty ? "–" : (counts[m] ?? 0)}</td>
        <td>${m === base ? `<span class="hint" style="margin:0">prop type</span>` : `<button type="button" class="small" data-vs-del="${esc(m)}">Remove</button>`}</td></tr>`).join("")}</tbody></table>
      <div class="row"><label for="vs-add">Add entry:</label><select id="vs-add">${free.length ? free.map(c => `<option>${esc(c)}</option>`).join("") : `<option value="">(no other prop entries: New entry… first)</option>`}</select>
        <button type="button" class="small" id="vs-add-btn"${free.length ? "" : " disabled"}>Add</button></div>
      <div class="row"><b>Areas:</b> ${areaNames.length ? areaNames.map(a => `<label class="check"><input type="checkbox" data-vs-area="${esc(a)}"${draft.areas.includes(a) ? " checked" : ""}> ${esc(a)}</label>`).join(" ") : ""}
        <span class="hint" style="margin:0">${draft.areas.length ? "only these areas' places pick variants" : "none ticked: every place picks variants"}</span></div>
      <p class="hint">Each place picks one entry by weight, the same one every time; adding an entry moves only some places to it.${dirty ? " Places are counted after Save." : ""}</p></fieldset>`;
    box.querySelectorAll("[data-vs-w]").forEach(el => el.onchange = () => { const x = parseFloat(el.value); draft.members[el.dataset.vsW] = x >= 0 ? x : 0; dirty = true; render(); });
    box.querySelectorAll("[data-vs-del]").forEach(el => el.onclick = () => { delete draft.members[el.dataset.vsDel]; dirty = true; render(); });
    box.querySelectorAll("[data-vs-area]").forEach(el => el.onchange = () => {
      draft.areas = el.checked ? [...draft.areas, el.dataset.vsArea] : draft.areas.filter(a => a !== el.dataset.vsArea); dirty = true; render(); });
    $("#vs-add-btn", box).onclick = () => { const v = $("#vs-add", box).value; if (v) { draft.members[v] = 1; dirty = true; render(); } };
  };
  render();
  return {save: async () => {
    if (!dirty || !draft) return;
    const base = info.set ? info.set.base : id;
    await api(`/api/projects/${slug}/library/variants/${encodeURIComponent(base)}`, {members: draft.members, areas: draft.areas});
  }};
}

// Manage entry… › Attached pieces (assembly.py): child entries placed with this one everywhere
// (Asset review preview, review renders, Unreal look scenes). Offsets are in this entry's frame:
// x right, y back (negative: in front), z up, metres, from its centre or the middle of its front
// edge; rotation about the vertical, degrees. The top-down view redraws as you type, and pieces
// can be dragged there. Saved with the dialog's Save (the assembly preview rebuilds in Blender).
async function childrenSection(id, box) {
  let info;
  try { info = await api(`/api/projects/${slug}/library/entries/${encodeURIComponent(id)}/children`, undefined, "GET"); }
  catch (err) { box.innerHTML = `<p class="error">${esc(errorText(err))}</p>`; return null; }
  if (info.attached_to) {
    box.innerHTML = `<p class="hint">Attached pieces: this entry is attached to <b>${esc(assetName(info.attached_to))}</b>, so it can't carry pieces of its own.</p>`;
    return null;
  }
  const natural = {};                          // entry -> its own size (before the piece's scale)
  const rows = info.children.map(c => { natural[c.entry] = c.size.map(v => v / c.scale); return {...c}; });
  let dirty = false;
  const par = info.parent.size, an = info.anchors;
  const others = libEntries().filter(e => e.id !== id).map(e => e.id).sort();
  const num = (v, d = 2) => +(+v).toFixed(d);
  const corners = r => {                        // a piece's footprint corners in the parent frame
    const [w, d] = [natural[r.entry][0] * r.scale, natural[r.entry][1] * r.scale];
    const a = an[r.anchor], cx = a[0] + r.offset[0], cy = a[1] + r.offset[1], t = r.yaw * Math.PI / 180;
    return [[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]]
      .map(([x, y]) => [cx + x * Math.cos(t) - y * Math.sin(t), cy + x * Math.sin(t) + y * Math.cos(t)]);
  };
  const svg = () => {
    const pts = [[-par[0] / 2, -par[1] / 2], [par[0] / 2, par[1] / 2], ...rows.flatMap(corners)];
    const xs = pts.map(p => p[0]), ys = pts.map(p => -p[1]);
    const m = Math.max(4, 0.08 * Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)));
    const vb = [Math.min(...xs) - m, Math.min(...ys) - m, Math.max(...xs) - Math.min(...xs) + 2 * m, Math.max(...ys) - Math.min(...ys) + 2 * m];
    const poly = (c, cls, extra = "") => `<polygon class="${cls}" ${extra} points="${c.map(([x, y]) => `${num(x)},${num(-y)}`).join(" ")}"/>`;
    const fs = vb[2] / 40;
    return `<svg id="ch-plan" viewBox="${vb.map(v => num(v)).join(" ")}" role="img" aria-label="Top-down placement of the attached pieces">
      ${poly([[-par[0] / 2, -par[1] / 2], [par[0] / 2, -par[1] / 2], [par[0] / 2, par[1] / 2], [-par[0] / 2, par[1] / 2]], "ch-parent")}
      <line class="ch-front" x1="${num(-par[0] / 2)}" y1="${num(par[1] / 2)}" x2="${num(par[0] / 2)}" y2="${num(par[1] / 2)}"/>
      <text x="${num(-par[0] / 2)}" y="${num(par[1] / 2 + fs * 1.4)}" font-size="${num(fs)}" text-anchor="start" class="ch-label">front edge (−y)</text>
      ${Object.entries(an).map(([k, a]) => `<circle class="ch-anchor" cx="${num(a[0])}" cy="${num(-a[1])}" r="${num(fs / 3)}"><title>${k} anchor</title></circle>`).join("")}
      ${rows.map((r, i) => { const c = corners(r), cx = c.reduce((s, p) => s + p[0], 0) / 4, cy = c.reduce((s, p) => s + p[1], 0) / 4;
        return poly(c, "ch-child", `data-i="${i}"`) + `<text x="${num(cx)}" y="${num(-cy)}" font-size="${num(fs)}" text-anchor="middle" class="ch-label">${esc(assetName(r.entry))}</text>`; }).join("")}
    </svg>`;
  };
  const st = info.assembly;
  const render = () => {
    box.innerHTML = `<fieldset id="ch-box"><legend>Attached pieces</legend>
      <p class="hint" style="margin-top:0">Entries placed with this one everywhere: Asset review preview, review renders and Unreal look scenes
        (separate actors under this one). x right, y back (negative is in front), z up, in metres from the anchor; rotation in degrees.</p>
      ${rows.length ? `<table class="grid-table" id="ch-table"><thead><tr><th>Piece</th><th>Anchor</th><th>x</th><th>y</th><th>z</th><th>Rotation</th><th>Scale</th><th></th></tr></thead><tbody>
        ${rows.map((r, i) => `<tr data-i="${i}"><td>${assetLabelHtml(r.entry)}${r.glb ? "" : ` <span class="hint" style="margin:0" title="No 3D model yet: a placeholder box in previews, left out in Unreal">(no model yet)</span>`}</td>
          <td><select class="ch-anchor-sel" style="min-width:8em" aria-label="Anchor of ${esc(r.entry)}">${["centre", "front"].map(k => `<option value="${k}"${r.anchor === k ? " selected" : ""}>${k === "front" ? "front edge" : "centre"}</option>`).join("")}</select></td>
          ${[0, 1, 2].map(k => `<td><input type="number" step="0.1" class="ch-off" data-k="${k}" value="${r.offset[k]}" aria-label="${"xyz"[k]} offset of ${esc(r.entry)} (m)" style="width:5.5em"></td>`).join("")}
          <td><input type="number" step="5" class="ch-yaw" value="${r.yaw}" aria-label="Rotation of ${esc(r.entry)} (degrees)" style="width:4.5em"></td>
          <td><input type="number" step="0.05" min="0.01" class="ch-scale" value="${r.scale}" aria-label="Scale of ${esc(r.entry)}" style="width:4.5em"></td>
          <td><button type="button" class="small ch-rm">Remove</button></td></tr>`).join("")}</tbody></table>` : `<p class="hint">None yet.</p>`}
      <div class="row"><select id="ch-add-sel" aria-label="Entry to attach"><option value="">attach an entry…</option>${others.map(e => `<option value="${esc(e)}">${esc(assetName(e))}${assetName(e) !== e ? ` (${esc(e)})` : ""}</option>`).join("")}</select>
        <button type="button" class="small" id="ch-add">Attach</button></div>
      <div id="ch-plan-box">${svg()}</div>
      <p class="hint" id="ch-status" style="margin:0">${!info.parent.glb ? "This entry has no 3D model yet: build it to see the assembly." :
        st ? (st.fresh ? "Assembly preview up to date" : "Assembly preview out of date: Save rebuilds it") + (st.missing && st.missing.length ? ` (placeholder boxes for ${st.missing.map(m => esc(assetName(m))).join(", ")})` : "") : ""}
        ${dirty ? " · unsaved changes" : ""}</p></fieldset>`;
    const redraw = () => { $("#ch-plan-box", box).innerHTML = svg(); wireDrag(); };
    box.querySelectorAll("#ch-table tr[data-i]").forEach(tr => {
      const r = rows[+tr.dataset.i];
      const set = f => e => { f(e.target.value); dirty = true; redraw(); };
      $(".ch-anchor-sel", tr).onchange = set(v => { r.anchor = v; });
      tr.querySelectorAll(".ch-off").forEach(inp => { inp.oninput = set(v => { r.offset[+inp.dataset.k] = +v || 0; }); });
      $(".ch-yaw", tr).oninput = set(v => { r.yaw = +v || 0; });
      $(".ch-scale", tr).oninput = set(v => { if (+v > 0) r.scale = +v; });
      $(".ch-rm", tr).onclick = () => { rows.splice(+tr.dataset.i, 1); dirty = true; render(); };
    });
    $("#ch-add", box).onclick = async () => {
      const e = $("#ch-add-sel", box).value; if (!e) return;
      try {
        const f = await api(`/api/projects/${slug}/library/entries/${encodeURIComponent(e)}/footprint`, undefined, "GET");
        natural[e] = f.size;
        rows.push({entry: e, anchor: "front", offset: [0, -num(f.size[1] / 2 + 2, 1), 0], yaw: 0, scale: 1, glb: f.has_model ? true : null});
        dirty = true; render();
      } catch (err) { alert(errorText(err)); }
    };
    const wireDrag = () => {
      const s = $("#ch-plan", box);
      s.querySelectorAll(".ch-child").forEach(pg => {
        pg.onpointerdown = ev => {
          const r = rows[+pg.dataset.i], ctm = s.getScreenCTM().inverse();
          const at = e => { const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm); return [p.x, -p.y]; };
          const [x0, y0] = at(ev), o0 = [...r.offset];
          pg.setPointerCapture(ev.pointerId);
          pg.onpointermove = e => {
            const [x, y] = at(e);
            r.offset[0] = num(o0[0] + x - x0, 1); r.offset[1] = num(o0[1] + y - y0, 1);
            const tr = box.querySelector(`#ch-table tr[data-i="${pg.dataset.i}"]`);
            if (tr) { tr.querySelector('.ch-off[data-k="0"]').value = r.offset[0]; tr.querySelector('.ch-off[data-k="1"]').value = r.offset[1]; }
            dirty = true; $("#ch-plan-box", box).innerHTML = svg(); wireDrag();
          };
          pg.onpointerup = () => { pg.onpointermove = null; };
        };
      });
    };
    wireDrag();
  };
  render();
  return {save: async () => {
    if (!dirty) return;
    const body = {children: rows.map(r => ({entry: r.entry, anchor: r.anchor, offset: r.offset, yaw: r.yaw, scale: r.scale}))};
    await api(`/api/projects/${slug}/library/entries/${encodeURIComponent(id)}/children`, body, "PUT");
  }};
}

// After Manage entry's Save: what a changed name or description affects. The description goes into
// the reference-sheet prompts (sw_library.asset_fields -> s2_refs); the procedural brief, Reroll
// and TRELLIS build from sheets and the brief, so it reaches them only through new sheets.
function entrySavedNotice(id, saved) {
  const lines = [];
  if (saved.some(s => s.startsWith("description")))
    lines.push(`Description saved${saved.includes("description") ? "" : " (back to the automatic one)"}. It is used the next time sheets are generated ` +
      "(Library › Reference sheets › Generate). The current brief, Reroll and Make 3D don't read it: to see it in 3D, generate new sheets, " +
      "then Brief & build from the one you like (or write what to change in Reroll's guidance).");
  if (saved.includes("form")) lines.push("Form and size saved. Used on the next Reroll (without guidance) or Brief & build, which make the brief " +
    "again from the sheet. A Reroll with guidance edits the current brief and keeps its form and size.");
  if (saved.includes("route")) lines.push("Build method saved. Used on the next build: procedural builds come from Reroll or Brief & build, " +
    "TRELLIS ones from Make 3D on Views and 3D.");
  if (saved.includes("name")) lines.push("Name saved: shown everywhere now; the next sheets use it too.");
  const b = document.createElement("div"); b.id = "entry-saved-body";
  b.innerHTML = lines.map(l => `<p>${esc(l)}</p>`).join("");
  dialog({title: `Saved: ${id}`, body: b, buttons: [{label: "OK", default: true}]}).win.id = "entry-saved-dialog";
}

async function manageEntry(id) {
  const ent = libEntries().find(x => x.id === id); if (!ent) return;
  const dec = libraryData.decisions[id] || {};
  const custom = !!ent.custom;
  const body = document.createElement("form"); body.className = "form"; body.id = "entry-form";
  const nm = dec.name || ent.name || id.replace(/-/g, " ");
  body.innerHTML = `<p class="hint" style="grid-column:1/-1">Entry <b>${esc(id)}</b>${custom ? " (yours)" : " (from the library plan: its id is fixed; rename changes the display name only, duplicate to get an id of your own)"}.</p>
    ${entryField("en-name", "Name:", `<span>${autoFieldHtml("input", `id="en-name"`, dec.name, ent.name || id.replace(/-/g, " "))}</span>`)}
    ${entryField("en-desc", "Description:", `<span>${autoFieldHtml("textarea", `id="en-desc" rows="4"`, dec.description, ent.description_auto || "")}</span>
      <span></span><span class="hint" style="margin:0">Sheets are drawn from it: the next Generate in Library › Reference sheets uses it. The brief and the 3D builds don't read it.</span>`)}
    ${entryField("en-route", "Build method:", `<select id="en-route"><option value="">auto (${LIB_ROUTES[ent.route_auto] || "none"})</option>
      <option value="procedural"${dec.route === "procedural" ? " selected" : ""}>procedural</option><option value="trellis"${dec.route === "trellis" ? " selected" : ""}>TRELLIS</option></select>`)}
    ${entryField("en-decision", "Decision:", `<select id="en-decision"><option value="">not decided</option>${LIB_DECISIONS.filter(([k]) => k !== "kit" || dec.decision === "kit")
      .map(([k, l]) => `<option value="${k}"${dec.decision === k ? " selected" : ""}${k === "source" && !ent.best ? " disabled" : ""}>${l}</option>`).join("")}</select>`)}
    <div class="ff-box" id="en-form-box" style="display:contents"></div>
    <div id="en-variants" style="grid-column:1/-1"></div>
    <div id="en-children" style="grid-column:1/-1"></div>`;
  let finfo = null;
  const vs = ent.kind === "prop" ? await variantSection(id, $("#en-variants", body)) : null;
  const kids = await childrenSection(id, $("#en-children", body));
  if ((ent.route || ent.route_auto) === "procedural" || dec.route === "procedural") {
    try { finfo = await api(`/api/projects/${slug}/library/entries/${id}/form`, undefined, "GET");
          $("#en-form-box", body).innerHTML = await formFieldsHtml(finfo, {allowAuto: true}); formFieldsWire(body); }
    catch (err) { $("#en-form-box", body).innerHTML = `<p class="error">${esc(errorText(err))}</p>`; }
  }
  const reload = async () => { await entriesReload(); };
  autoFieldsWire(body);
  const d = dialog({title: `Entry: ${id}`, body, className: "wide", buttons: [
    {label: "Save", default: true, onClick: async () => {
      const saved = [];
      try {
        const desc = autoFieldValue($("#en-desc", body)), nameEl = $("#en-name", body);
        const name = nameEl.value.trim() || nameEl.dataset.auto;      // no "unset" for a name: Reset puts the automatic text back
        if (desc !== (dec.description || null)) {
          await api(`/api/projects/${slug}/library/entries/${id}/description`, {description: desc});
          saved.push(desc ? "description" : "description (back to automatic)");
        }
        if (name !== nm) { await api(`/api/projects/${slug}/library/entries/${id}/rename`, {name}); saved.push("name"); }
        const route = $("#en-route", body).value || null;
        if (route !== (dec.route || null)) { await api(`/api/projects/${slug}/library/route`, {entry: id, route}); saved.push("route"); }
        if (finfo) {
          const ff = formFieldsRead(body);
          const same = (a, b) => JSON.stringify(a || null) === JSON.stringify(b || null);
          const mySize = finfo.size_source === "yours" ? finfo.size_m : null;
          if (ff.form || !same(ff.dims, mySize) && !same(ff.dims, finfo.size_m)) {
            const myForm = finfo.source === "yours" ? finfo.form : null, size_m = ff.dims && !same(ff.dims, finfo.size_m) ? ff.dims : mySize;
            await api(`/api/projects/${slug}/library/entries/${id}/form`, {form: ff.form || myForm, size_m});
            if ((ff.form || myForm) !== myForm || !same(size_m, mySize)) saved.push("form");
          }
        }
        if (vs) await vs.save();
        if (kids) await kids.save();
        const decision = $("#en-decision", body).value || null;
        if (decision !== (dec.decision || null)) {
          if (decision === "reference") { alert("Pick the image with the card's 'Description + chosen image' button."); }
          else await api(`/api/projects/${slug}/library/decision`, {entry: id, decision});
        }
      } catch (err) { alert(errorText(err)); return false; }
      await reload();
      if (saved.length) entrySavedNotice(id, saved);
    }},
    {label: "Duplicate…", onClick: () => { setTimeout(() => idPrompt({title: `Duplicate ${id}`, label: "New id:", value: `${id}-copy`, button: "Duplicate",
      intro: "Copies the entry with its description, route, decision, sheets, views, brief, build and TRELLIS results. The copy starts unreviewed; the original is untouched.",
      onGo: async (to, name) => { const r = await entriesCall(`/${id}/duplicate`, {to, name: name || null}); if (r) await reload(); return !!r; }}), 0); }},
    {label: "Rename…", onClick: () => { setTimeout(() => {
      if (custom) idPrompt({title: `Rename ${id}`, label: "New id:", value: id, button: "Rename",
        intro: "Moves the entry's sheets, views, brief, build, results and versions to the new id (files are renamed, not copied) and keeps its review. Variant sets and area checklists follow; what Unreal still names by the old id is listed afterwards.",
        onGo: async (to, name) => {
          if (to !== id && !(await askDialog("Rename entry", `Rename ${id} to ${to}? Its files move to the new id.`, "Rename"))) return false;
          const r = await entriesCall(`/${id}/rename`, {to, name: name || null}); if (r) await reload();
          if (r && (r.not_updated || []).length) setTimeout(() => alert(`Renamed ${id} to ${r.id}. Not updated (still the old id):\n\n• ${r.not_updated.join("\n• ")}`), 0);
          return !!r; }});
      else {
        const f = document.createElement("form"); f.className = "form";
        f.innerHTML = `<p class="hint" style="grid-column:1/-1">${esc(id)} comes from the library plan, so its id stays; only the display name changes.</p>
          ${entryField("en-newname", "Name:", `<input id="en-newname" value="${esc(nm)}" required>`)}`;
        dialog({title: `Rename ${id}`, body: f, buttons: [{label: "Rename", default: true, onClick: async () => {
          if (!f.reportValidity()) return false;
          const r = await entriesCall(`/${id}/rename`, {name: $("#en-newname", f).value.trim()}); if (!r) return false; await reload(); }},
          {label: "Cancel"}]});
      }
    }, 0); }},
    {label: "Close"}]});
}
$("#lib-new").onclick = newEntryDialog;
