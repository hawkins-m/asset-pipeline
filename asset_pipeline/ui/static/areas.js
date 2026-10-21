// Library › Areas (the Library tab's first sub-tab; Library › Areas… in the menu opens it): what
// `ap area define|status|checklist` and the area batch commands do.
// An area is a set of shots and/or districts; the table shows what each asset of it has and its
// next step, with a button that performs the step. Server side: tools_api.py (/areas...).
const AREA_BATCHES = {
  sheets: ["Sheets for missing", "Two reference sheets for every decided asset without any", "Reference sheets are made on GPU 1 (ComfyUI)."],
  build: ["Build all", "Build every procedural asset with a brief", "Blender on the CPU; approved assets and unchanged briefs are skipped."],
  trellis: ["TRELLIS all", "3D for every prop with a chosen view", "GPU 0; the vision LLM is stopped while TRELLIS runs. Assets need a chosen view."],
  instances: ["Instances", "Every building at its own footprint and storeys", "Blender on the CPU; one build per building footprint."],
};
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
      `<button type="button" class="small" id="area-batch-${k}" data-batch="${k}" title="${esc(v[1])}: ${esc(v[2])}">${v[0]}…</button>`).join("")}
      <button type="button" class="small" id="area-checklist">Write checklist</button></div>`;
  body.insertAdjacentHTML("afterbegin", `<p class="hint" style="margin-top:0">An area is a set of shots and/or districts. Each row
    is one asset of it, with what it has and its <b>Next</b> step: the button does that step.</p>`);
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
    try { const st = await api(`/api/projects/${slug}/areas/status?${q}`, undefined, "GET"); areaState.rows = st.rows; areaState.gaps = st.gaps || []; }
    catch (err) { box.innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
    areaGaps(d, table);
    box.className = "";
    if (!areaState.rows.length) { box.innerHTML = `<p class="hint">No assets in this area.</p>`; return; }
    box.innerHTML = `<table class="grid-table" id="area-table"><thead><tr><th>Asset</th><th>Tier</th><th>Build method</th><th>Share</th>
      <th>Sheets</th><th>Brief</th><th>Build</th><th>Review</th><th>Next</th></tr></thead><tbody>${areaState.rows.map((r, i) => {
        const st = areaStep(r);
        return `<tr data-asset="${esc(r.asset)}"><td>${assetLabelHtml(r.asset)}${r.variant_of ? ` <span class="hint" style="margin:0" title="a member of ${esc(r.variant_of)}'s variant set (Manage entry… › Variant set)">variant of ${esc(assetName(r.variant_of))}</span>` : ""}</td><td>${esc(r.tier || "")}</td><td>${esc(r.route)}</td>
          <td>${(100 * r.share).toFixed(1)}%</td><td>${r.sheets}</td><td>${r.brief ? "yes" : (r.missing || []).length
            ? `<span class="error" title="Brief & build needs a building form and a size: set them in Manage entry… (or in the Brief & build dialog)">no ${esc(r.missing.join(", no "))}</span>` : "–"}</td><td>${areaBuildText(r)}</td>
          <td>${esc(r.status || "not reviewed")}</td><td><button type="button" class="small area-step" data-i="${i}" data-step="${st.kind}"
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

// Batches confirm first with counts (a dry run asks the server what would run).
async function areaBatch(kind, assets, d, table) {
  const [title, cmd, note] = AREA_BATCHES[kind];
  const req = {action: kind, ...areaScope(), assets, more: false};
  let plan;
  try { plan = await api(`/api/projects/${slug}/areas/batch`, {...req, dry_run: true}); }
  catch (err) { alert(errorText(err)); return; }
  const ok = await confirmDialog({title, button: "Run", id: "area-batch-confirm", body: `<p><b>${plan.count}</b> to run: ${esc(plan.items.slice(0, 40).join(", "))}${plan.items.length > 40 ? " …" : ""}</p>
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
