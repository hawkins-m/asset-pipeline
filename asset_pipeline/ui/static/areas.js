// Library › Areas (the Library tab's first sub-tab; Library › Areas… in the menu opens it): what
// `ap area define|status|checklist` and the area batch commands do.
// An area is a set of shots and/or districts; the table shows what each asset of it has and its
// next step, with a button that performs the step. Server side: tools_api.py (/areas...).
const AREA_BATCHES = {
  sheets: ["Sheets for missing", "Two reference sheets for each decided entry of this area that has none yet",
    "Reference sheets are made on GPU 1 (ComfyUI). Skips approved, undecided or excluded entries, those with sheets, and procedural assets that already have a brief."],
  build: ["Build all", "Builds each procedural asset of this area that has a brief and isn't fresh",
    "Blender on the CPU. Skips approved assets, fresh builds (made from the current brief), assets without a brief and TRELLIS assets."],
  trellis: ["TRELLIS all", "Makes a TRELLIS model for each asset of this area whose build method is already trellis and has no model yet",
    "GPU 0; the vision LLM is stopped while TRELLIS runs. Skips approved assets, assets without a chosen view and every procedural asset; no build method is changed."],
  instances: ["Instances", "Builds every building of these procedural entries at its own footprint and storeys",
    "Blender on the CPU, one build per building in the whole city plan (not only this area). Needs a brief; unchanged builds are skipped."],
};
// column headers and value words: what they mean (areas.py status rows, sw_library tiers)
const AREA_HEADS = {
  Asset: "The library asset: an entry, or one module of a kit entry. \"variant of\" marks a member of another entry's variant set.",
  Tier: "How the library plan classed the entry: A landmark or kit monument, B one-off civic or infrastructure, C repeated fill typology, D prop. It sets the default build method, how the size is taken and how entries are grouped.",
  "Build method": "How its 3D model is made: procedural (Blender from a brief measured off a sheet) or trellis (TRELLIS.2 from a chosen sheet view). Change it in Manage entry….",
  Share: "The largest share of one frame this asset covers in the area's shots (its greybox slots in the shot's id pass); props take the share of the whole slot holding them (a market stall: the market), so theirs is approximate. 0% means it is only in the area's districts.",
  Sheets: "How many reference sheets the asset has (Library › Reference sheets).",
  Brief: "Procedural assets: whether it has a brief, the fields Blender builds from. \"no form\" or \"no size\": it can't be briefed until that is set (Manage entry…).",
  Build: "fresh: the procedural build was made from the current brief. stale: the brief changed since the last build. TRELLIS: it has a TRELLIS model. –: not built yet.",
  Review: "Its status in Library › Asset review: approved, needs changes, needs new feature, ready for review, needs review, or not reviewed.",
  Next: "The asset's next step; the button does it.",
};
const AREA_TIERS = {
  A: "Tier A: a landmark or kit monument (its catalog typology has a reference image, or it is a temple, stoa, rotunda and the like). Sized from its smallest placed instance; procedural unless it is a drum form, rotunda, fountain or statue.",
  B: "Tier B: a one-off civic or infrastructure building (a wall, gate or quay, or a catalog type with a singular form or at most 14 copies). Sized from its smallest placed instance; procedural by default.",
  C: "Tier C: a repeated fill typology from the catalog, one entry per typology and material shared by all its buildings. Sized from the catalog's middle size; procedural by default.",
  D: "Tier D: a prop (a market stall, tent or canopy table). A fixed prop size, TRELLIS by default and a lighter look reference on its sheets.",
};
const AREA_ROUTES = {procedural: "Built in Blender from a brief measured off its sheet.", trellis: "Built by TRELLIS.2 from one chosen sheet view (Views and 3D)."};
const AREA_BUILDS = {fresh: "Built from the current brief: nothing to rebuild.", stale: "The brief changed since the last build: Build makes it again.",
  TRELLIS: "Has a TRELLIS model (Views and 3D).", "–": "Not built yet."};
const AREA_REVIEWS = {approved: "Approved: done; Unreal look scenes place it.", needs_changes: "Needs changes: a reviewer asked for another build (the note is in Asset review).",
  needs_feature: "Needs new feature: the builder lacks a part this asset needs.", ready: "Ready for review: built and waiting for a review.",
  needs_review: "Needs review: changed after it was approved; review it again.", "not reviewed": "Not reviewed yet."};
const areaState = {sel: null, data: null, rows: [], gaps: []};
// the area picked here is the "current area" (Asset review's Show filter defaults to it)
function rememberArea() {
  if (areaState.sel && areaState.sel.startsWith("a:")) {
    try { localStorage.setItem(`ap.area.${slug}`, areaState.sel.slice(2)); } catch (_) { /* ignore */ }
  }
}

function areaStep(r) {
  if (r.status === "approved") return {kind: "done", label: "Done"};
  if (!r.decision && !r.brief) return {kind: "decide", label: "Decide…"};
  if (!r.sheets && !(r.route === "procedural" && r.brief)) return {kind: "sheets", label: "Make sheets"};
  if (r.route === "procedural") {
    if (!r.brief && (r.missing || []).length) return {kind: "form", label: "Set form…"};
    if (!r.brief) return {kind: "brief", label: "Brief + build"};
    if (!r.built || !r.fresh) return {kind: "build", label: "Build"};
    return {kind: "review", label: r.status ? "Open review" : "Review…"};
  }
  if (!r.trellis) return {kind: "trellis", label: "Run TRELLIS"};
  return {kind: "review3d", label: "Review 3D…"};
}
const areaBuildText = r => r.built ? (r.fresh ? "fresh" : "stale") : r.trellis ? "TRELLIS" : "–";
const areaScope = () => areaState.sel && areaState.sel.startsWith("d:") ? {district: areaState.sel.slice(2)} : {area: (areaState.sel || "").slice(2)};

// Rendered when the sub-tab is shown (or the project changes), not on every poll: the table is
// loaded from /areas/status, and steps and batches reload it themselves.
const areasShown = {slug: null};
function areasDialog() {          // the menu item: open the sub-tab
  if (noProject()) return;
  showTab("library"); showSub("areas");
}
function areasRefresh(force = false) {
  const host = $("#areas-tab");
  if (!host || host.hidden || !slug) return;
  if (!force && areasShown.slug === slug && host.children.length) return;
  areasShown.slug = slug;
  areasPanel(host);
}
async function areasPanel(host) {
  const body = document.createElement("div"); body.id = "areas-body";
  body.innerHTML = `<div class="row"><label for="area-select">Area:</label><select id="area-select"></select>
      <button type="button" id="area-new" class="small">New area…</button>
      <button type="button" id="area-refresh" class="small">Refresh</button></div>
    <div id="area-define" hidden></div>
    <div id="area-gaps" hidden></div>
    <div id="area-table-box" class="hint">Loading…</div>
    <div class="row" id="area-batches">${Object.entries(AREA_BATCHES).map(([k, v]) =>
      `<button type="button" class="small" id="area-batch-${k}" data-batch="${k}" title="${esc(v[1])}. ${esc(v[2])}">${v[0]}…</button>`).join("")}
      <button type="button" class="small" id="area-checklist" title="Writes this table as a checklist to areas/<name>.md in the project, keeping your own notes around it. Named areas only.">Write checklist</button></div>`;
  body.insertAdjacentHTML("afterbegin", `<p class="hint" style="margin-top:0" id="area-help" title="An area is a set of shots and/or districts. Each row is one asset of it, with what it has so far and its Next step: the button does that step. The batch buttons below run one step for every asset of the area that needs it.">One row per asset of the area; <b>Next</b> does its next step. Hover any header or value for details.</p>`);
  host.replaceChildren(body);
  const d = {win: host, close() {}};      // the steps that open another tab "close" the panel: nothing to do
  const reload = async () => {
    try { areaState.data = await api(`/api/projects/${slug}/areas`, undefined, "GET"); }
    catch (err) { $("#area-table-box", d.win).innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
    const a = areaState.data;
    const opts = [...Object.keys(a.areas).map(n => ["a:" + n, n]), ...a.districts.map(x => ["d:" + x, `district ${x}`])];
    const sel = $("#area-select", d.win);
    sel.innerHTML = opts.length ? opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("") : `<option value="">(no areas yet)</option>`;
    if (areaState.sel && opts.some(o => o[0] === areaState.sel)) sel.value = areaState.sel; else areaState.sel = opts.length ? opts[0][0] : null;
    sel.value = areaState.sel || "";
    await table();
  };
  const table = async () => {
    const box = $("#area-table-box", d.win);
    $("#area-checklist", d.win).disabled = !(areaState.sel || "").startsWith("a:");
    d.win.querySelectorAll("[data-batch]").forEach(b => b.disabled = !areaState.sel);
    if (!areaState.sel) { box.textContent = "Define an area (New area…) from shots and/or districts."; return; }
    box.textContent = "Loading…";
    const q = new URLSearchParams(areaScope());
    try { const st = await api(`/api/projects/${slug}/areas/status?${q}`, undefined, "GET"); areaState.rows = st.rows; areaState.gaps = st.gaps || []; areaBatchLabels(d, st.batches || {}); }
    catch (err) { box.innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
    areaGaps(d, table);
    box.className = "";
    if (!areaState.rows.length) { box.innerHTML = `<p class="hint">No assets in this area.</p>`; return; }
    const tip = t => t ? ` title="${esc(t)}"` : "";
    box.innerHTML = `<table class="grid-table" id="area-table"><thead><tr>${Object.entries(AREA_HEADS).map(([h, t]) => `<th${tip(t)}>${esc(h)}</th>`).join("")}</tr></thead><tbody>${areaState.rows.map((r, i) => {
        const st = areaStep(r), rv = r.status || "not reviewed", bt = areaBuildText(r);
        return `<tr data-asset="${esc(r.asset)}"><td>${assetLabelHtml(r.asset)}${r.variant_of ? ` <span class="hint" style="margin:0" title="a member of ${esc(r.variant_of)}'s variant set (Manage entry… › Variant set)">variant of ${esc(assetName(r.variant_of))}</span>` : ""}</td><td${tip(AREA_TIERS[r.tier])}>${esc(r.tier || "")}</td><td${tip(AREA_ROUTES[r.route])}>${esc(r.route)}</td>
          <td title="${esc(`${(100 * r.share).toFixed(2)}% of its best frame in this area's shots`)}">${(100 * r.share).toFixed(1)}%</td><td>${r.sheets}</td><td${tip(r.brief ? "Has a brief: the fields Blender builds from." : (r.missing || []).length ? "" : r.route === "procedural" ? "No brief yet: Brief & build makes one from a sheet." : "TRELLIS assets need no brief.")}>${r.brief ? "yes" : (r.missing || []).length
            ? `<span class="error" title="It can't be briefed until it has a building ${esc(r.missing.join(" and a "))}: set ${r.missing.length > 1 ? "them" : "it"} in Manage entry… (or the Brief & build dialog).">no ${esc(r.missing.join(", no "))}</span>` : "–"}</td><td${tip(AREA_BUILDS[bt])}>${bt}</td>
          <td${tip((AREA_REVIEWS[rv] || "") + (r.note ? ` Note: ${r.note}` : ""))}>${esc(rv)}</td><td><button type="button" class="small area-step" data-i="${i}" data-step="${st.kind}"
            title="${esc(r.next)}" ${st.kind === "done" ? "disabled" : ""}>${esc(st.label)}</button></td></tr>`; }).join("")}</tbody></table>`;
    box.querySelectorAll(".area-step").forEach(b => b.onclick = () => areaDoStep(areaState.rows[+b.dataset.i], d, table));
  };
  $("#area-select", d.win).onchange = e => { areaState.sel = e.target.value || null; rememberArea(); table(); };
  $("#area-refresh", d.win).onclick = () => reload();
  $("#area-new", d.win).onclick = () => areaDefineForm(d, reload);
  $("#area-checklist", d.win).onclick = async () => {
    try {
      const r = await api(`/api/projects/${slug}/areas/${encodeURIComponent(areaState.sel.slice(2))}/checklist`, {});
      textDialog(`Checklist: ${r.rel}`, r.text, "area-checklist-dialog");
    } catch (err) { alert(errorText(err)); }
  };
  d.win.querySelectorAll("[data-batch]").forEach(b => b.onclick = () => areaBatch(b.dataset.batch, null, d, table));
  await reload();
}

function areaDefineForm(d, reload) {
  const a = areaState.data, box = $("#area-define", d.win);
  box.hidden = false;
  box.innerHTML = `<fieldset><legend>New area</legend>
    <div class="row"><label for="area-name">Name:</label><input id="area-name" placeholder="market-street" pattern="[a-z0-9](?:[a-z0-9]|-)*"></div>
    <div class="row"><b>Shots:</b> ${a.shots.length ? a.shots.map(s => `<label class="check"><input type="checkbox" name="area-shot" value="${esc(s)}"> ${esc(s)}</label>`).join(" ")
      : `<span class="hint">no shots yet</span>`}</div>
    <div class="row"><b>Districts:</b> ${a.districts.length ? a.districts.map(s => `<label class="check"><input type="checkbox" name="area-district" value="${esc(s)}"> ${esc(s)}</label>`).join(" ")
      : `<span class="hint">no districts</span>`}</div>
    <div class="row"><button type="button" id="area-save" class="default">Define</button> <button type="button" id="area-cancel">Cancel</button></div></fieldset>`;
  $("#area-cancel", box).onclick = () => { box.hidden = true; box.replaceChildren(); };
  $("#area-save", box).onclick = async () => {
    const name = $("#area-name", box).value.trim();
    const pick = n => [...box.querySelectorAll(`[name=${n}]:checked`)].map(x => x.value);
    try {
      await api(`/api/projects/${slug}/areas`, {name, shots: pick("area-shot"), districts: pick("area-district")});
      areaState.sel = "a:" + name; rememberArea(); box.hidden = true; box.replaceChildren(); await reload();
    } catch (err) { alert(errorText(err)); }
  };
  $("#area-name", box).focus();
}

// Form gaps: procedural assets with no form (or size) yet, each with its suggested fixes as
// buttons (the first is the recommended one). A form fix sets the entry's form and size, a route
// fix moves it to TRELLIS, "Set size…" opens Manage entry.
function areaGaps(d, table) {
  const box = $("#area-gaps", d.win), gaps = areaState.gaps;
  box.hidden = !gaps.length;
  if (!gaps.length) { box.replaceChildren(); return; }
  box.innerHTML = `<fieldset id="area-gap-list"><legend>Form gaps (${gaps.length})</legend>
    <p class="hint" style="margin-top:0">These can't be briefed yet. The first button is the suggested fix.</p>
    ${gaps.map((g, i) => `<div class="row area-gap" data-asset="${esc(g.asset)}"><b style="min-width:13em" title="${esc(g.asset)}">${esc(assetName(g.asset))}</b>
      <span class="error" style="min-width:6em">no ${esc(g.missing.join(", no "))}</span>
      ${g.fixes.map((f, k) => `<button type="button" class="small${k === 0 ? " default" : ""} area-fix" data-g="${i}" data-f="${k}"
        title="${esc(f.why || "")}">${esc(f.label)}</button>`).join(" ")}
      <button type="button" class="small area-fix-manage" data-g="${i}">Manage entry…</button></div>`).join("")}</fieldset>`;
  box.querySelectorAll(".area-fix").forEach(b => b.onclick = () => areaFix(gaps[+b.dataset.g], gaps[+b.dataset.g].fixes[+b.dataset.f], d, table));
  box.querySelectorAll(".area-fix-manage").forEach(b => b.onclick = () => areaFix(gaps[+b.dataset.g], {kind: "manage"}, d, table));
}

async function areaFix(g, f, d, table) {
  try {
    if (f.kind === "form") await api(`/api/projects/${slug}/library/entries/${encodeURIComponent(g.asset)}/form`, {form: f.form, size_m: f.size_m || null});
    else if (f.kind === "route") await api(`/api/projects/${slug}/library/route`, {entry: g.entry, route: f.route});
    else {
      const row = areaState.rows.find(r => r.asset === g.asset) || {asset: g.asset, entry: g.entry};
      d.close(); showTab("library");
      if (row.asset === row.entry) { showSub("assets"); await manageEntry(row.asset); return; }
      showSub("refs"); return;
    }
  } catch (err) { alert(errorText(err)); return; }
  await load(); table();
}

// The batch buttons show how many assets each would run now and, on hover, which and what is skipped.
function areaBatchLabels(d, batches) {
  for (const [k, [label, what, note]] of Object.entries(AREA_BATCHES)) {
    const b = $(`#area-batch-${k}`, d.win), p = batches[k];
    if (!b || !p) continue;
    const n = p.items.length, list = p.items.slice(0, 12).join(", ") + (n > 12 ? `, … (${n - 12} more)` : "");
    b.textContent = `${label} (${n})…`;
    b.disabled = !n;
    b.title = `${what}. ${n ? `Runs on: ${list}.` : "Nothing to run in this area now."}${p.notes.length ? ` ${p.notes.map(x => x[0].toUpperCase() + x.slice(1)).join(". ")}.` : ""} ${note}`;
  }
}

// Batches confirm first with counts (a dry run asks the server what would run).
async function areaBatch(kind, assets, d, table) {
  const [title, cmd, note] = AREA_BATCHES[kind];
  const req = {action: kind, ...areaScope(), assets, more: false};
  let plan;
  try { plan = await api(`/api/projects/${slug}/areas/batch`, {...req, dry_run: true}); }
  catch (err) { alert(errorText(err)); return; }
  const ok = await confirmDialog({title: `${title} (${plan.count})`, button: "Run", id: "area-batch-confirm", body: `<p>${esc(cmd)}.</p><p><b>${plan.count}</b> to run: ${esc(plan.items.slice(0, 40).join(", "))}${plan.items.length > 40 ? " …" : ""}</p>
    ${plan.notes.map(n => `<p class="hint">${esc(n)}</p>`).join("")}<p class="hint">${esc(note)} Finished items are skipped, so it can be run again.</p>`});
  if (!ok) return;
  try { const j = await api(`/api/projects/${slug}/areas/batch`, req); await load(); await jobDone(j); }
  catch (err) { alert(errorText(err)); }
  await load(); table();
}

async function areaDoStep(r, d, table) {
  const step = areaStep(r).kind;
  try {
    if (step === "decide") {
      d.close(); showTab("library"); showSub("assets");
      const card = document.querySelector(`#assets-list [data-entry="${CSS.escape(r.entry)}"]`);
      if (card) { card.scrollIntoView({block: "center"}); card.classList.add("flash"); }
      return;
    }
    if (step === "form") {
      d.close(); showTab("library");
      if (r.asset === r.entry) { showSub("assets"); await manageEntry(r.asset); return; }
      showSub("refs");                          // a kit module: its Brief & build dialog asks for form and size
      const row = document.querySelector(`#refs-list .next-step[data-unit="${CSS.escape(r.asset)}"]`);
      if (row) row.scrollIntoView({block: "center"});
      return;
    }
    if (step === "review") { window.open(`/?project=${encodeURIComponent(slug)}&review=${encodeURIComponent(r.asset)}`, "_blank"); return; }
    if (step === "review3d") { d.close(); showTab("views"); return; }
    if (step === "sheets" || step === "trellis") return areaBatch(step, [r.asset], d, table);
    const j = await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(r.asset)}/brief-build`, {});   // brief + build, or build
    await load(); await jobDone(j);
  } catch (err) { alert(errorText(err)); }
  await load(); table();
}

Object.assign(ACTIONS, {areas: {run: areasDialog, enabled: () => !!slug}});
