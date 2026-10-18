// Library › Areas…: what `ap area define|status|checklist` and the area batch commands do.
// An area is a set of shots and/or districts; the table shows what each asset of it has and its
// next step, with a button that performs the step. Server side: tools_api.py (/areas...).
const AREA_BATCHES = {
  sheets: ["Sheets for missing", "ap library sheets --area", "Reference sheets are made on GPU 1 (ComfyUI)."],
  build: ["Build all", "ap library build --area", "Blender on the CPU; approved assets and unchanged briefs are skipped."],
  trellis: ["TRELLIS all", "ap library trellis --area", "GPU 0; the vision LLM is stopped while TRELLIS runs. Assets need a chosen view."],
  instances: ["Instances", "ap library instances --area", "Blender on the CPU; one build per building footprint."],
};
const areaState = {sel: null, data: null, rows: []};

function areaStep(r) {
  if (r.status === "approved") return {kind: "done", label: "Done"};
  if (!r.decision && !r.brief) return {kind: "decide", label: "Decide…"};
  if (!r.sheets && !(r.route === "procedural" && r.brief)) return {kind: "sheets", label: "Make sheets"};
  if (r.route === "procedural") {
    if (!r.brief) return {kind: "brief", label: "Brief + build"};
    if (!r.built || !r.fresh) return {kind: "build", label: "Build"};
    return {kind: "review", label: r.status ? "Open review" : "Review…"};
  }
  if (!r.trellis) return {kind: "trellis", label: "Run TRELLIS"};
  return {kind: "review3d", label: "Review 3D…"};
}
const areaBuildText = r => r.built ? (r.fresh ? "fresh" : "stale") : r.trellis ? "TRELLIS" : "–";
const areaScope = () => areaState.sel && areaState.sel.startsWith("d:") ? {district: areaState.sel.slice(2)} : {area: (areaState.sel || "").slice(2)};

async function areasDialog() {
  if (noProject()) return;
  const body = document.createElement("div"); body.id = "areas-body";
  body.innerHTML = `<div class="row"><label for="area-select">Area:</label><select id="area-select"></select>
      <button type="button" id="area-new" class="small">New area…</button>
      <button type="button" id="area-refresh" class="small">Refresh</button></div>
    <div id="area-define" hidden></div>
    <div id="area-table-box" class="hint">Loading…</div>
    <div class="row" id="area-batches">${Object.entries(AREA_BATCHES).map(([k, v]) =>
      `<button type="button" class="small" id="area-batch-${k}" data-batch="${k}" title="${esc(v[1])}: ${esc(v[2])}">${v[0]}…</button>`).join("")}
      <button type="button" class="small" id="area-checklist">Write checklist</button></div>`;
  const d = dialog({title: "Areas", body, className: "tool-window areas-window", buttons: [{label: "Close", default: true}]});
  d.win.id = "areas-dialog";
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
    try { areaState.rows = (await api(`/api/projects/${slug}/areas/status?${q}`, undefined, "GET")).rows; }
    catch (err) { box.innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
    box.className = "";
    if (!areaState.rows.length) { box.innerHTML = `<p class="hint">No assets in this area.</p>`; return; }
    box.innerHTML = `<table class="grid-table" id="area-table"><thead><tr><th>Asset</th><th>Tier</th><th>Route</th><th>Share</th>
      <th>Sheets</th><th>Brief</th><th>Build</th><th>Review</th><th>Next</th></tr></thead><tbody>${areaState.rows.map((r, i) => {
        const st = areaStep(r);
        return `<tr data-asset="${esc(r.asset)}"><td>${esc(r.asset)}</td><td>${esc(r.tier || "")}</td><td>${esc(r.route)}</td>
          <td>${(100 * r.share).toFixed(1)}%</td><td>${r.sheets}</td><td>${r.brief ? "yes" : "–"}</td><td>${areaBuildText(r)}</td>
          <td>${esc(r.status || "not reviewed")}</td><td><button type="button" class="small area-step" data-i="${i}" data-step="${st.kind}"
            title="${esc(r.next)}" ${st.kind === "done" ? "disabled" : ""}>${esc(st.label)}</button></td></tr>`; }).join("")}</tbody></table>`;
    box.querySelectorAll(".area-step").forEach(b => b.onclick = () => areaDoStep(areaState.rows[+b.dataset.i], d, table));
  };
  $("#area-select", d.win).onchange = e => { areaState.sel = e.target.value || null; table(); };
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
    <div class="row"><label for="area-name">Name:</label><input id="area-name" placeholder="market-street" pattern="[a-z0-9][a-z0-9-]*"></div>
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
      areaState.sel = "a:" + name; box.hidden = true; box.replaceChildren(); await reload();
    } catch (err) { alert(errorText(err)); }
  };
  $("#area-name", box).focus();
}

// Batches confirm first with counts (a dry run asks the server what would run).
async function areaBatch(kind, assets, d, table) {
  const [title, cmd, note] = AREA_BATCHES[kind];
  const req = {action: kind, ...areaScope(), assets, more: false};
  let plan;
  try { plan = await api(`/api/projects/${slug}/areas/batch`, {...req, dry_run: true}); }
  catch (err) { alert(errorText(err)); return; }
  const ok = await confirmDialog({title, button: "Run", id: "area-batch-confirm", body: `<p><b>${plan.count}</b> to run: ${esc(plan.items.slice(0, 40).join(", "))}${plan.items.length > 40 ? " …" : ""}</p>
    ${plan.notes.map(n => `<p class="hint">${esc(n)}</p>`).join("")}<p class="hint">${esc(note)} Same as <code>${esc(cmd)} NAME</code>. Finished items are skipped, so it can be run again.</p>`});
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
    if (step === "review") { window.open(`/?project=${encodeURIComponent(slug)}&review=${encodeURIComponent(r.asset)}`, "_blank"); return; }
    if (step === "review3d") { d.close(); showTab("views"); return; }
    if (step === "sheets" || step === "trellis") return areaBatch(step, [r.asset], d, table);
    const j = await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(r.asset)}/brief-build`, {});   // brief + build, or build
    await load(); await jobDone(j);
  } catch (err) { alert(errorText(err)); }
  await load(); table();
}

Object.assign(ACTIONS, {areas: {run: areasDialog, enabled: () => !!slug}});
