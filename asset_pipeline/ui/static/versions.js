// Asset version bank (server side: asset_pipeline/versions.py, ui/versions_api.py).
// - versionStrip(box, asset, onChange): the strip of an asset's builds (Library › Asset review
//   detail view, and Views and 3D for library props). Per version: thumbnail, vN, date, source
//   (sheet, view, guidance, seed), size, Restore / Pin / Delete, two ticked = Compare.
// - Tools › Storage…: what the builds and versions take, per asset and per project, with
//   "Delete unpinned versions" (never the Approved, pinned or current ones).
// - Tools › Settings…: how many unpinned versions to keep per asset.
// Loaded after tools.js (jobDone, confirmDialog).
Object.assign(JOB_LABELS, {"library.restore": "restoring a version"});
Object.assign(LOADERS, {"library.restore": ["Restoring the version…", "greybox", "steps"]});

const fmtBytes = b => b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : b >= 1e3 ? `${Math.round(b / 1e3)} KB` : `${b || 0} B`;
const verDate = s => (s || "").replace("T", " ").slice(0, 16);
const verBase = p => String(p || "").split("/").pop();
const verSource = v => {
  const s = v.source || {}, bits = [];
  if (s.sheet) bits.push(verBase(s.sheet).replace(".png", "") + (s.position != null && s.position !== "" ? ` v${s.position}` : ""));
  if (s.form) bits.push(s.form);
  if (s.seed != null) bits.push(`seed ${s.seed}`);
  if (s.mode) bits.push(s.mode);
  if (s.guidance) bits.push(`“${s.guidance.length > 60 ? s.guidance.slice(0, 57) + "…" : s.guidance}”`);
  return bits.join(" · ");
};

async function versionJob(job) {        // a started job: wait (api() has opened its progress dialog), then say if it failed
  const j = await jobDone(job);
  if (j.status === "error") alert(errorText(j.error));
  return j;
}

// box: the element, or a selector looked up again after the request (the view may have been
// re-rendered meanwhile, leaving the first element detached and the strip empty)
async function versionStrip(boxOrSel, asset, onChange) {
  const find = () => typeof boxOrSel === "string" ? document.querySelector(boxOrSel) : boxOrSel;
  if (!find()) return;
  let d, err = null;
  try { d = await api(`/api/projects/${slug}/library/asset/${asset}/versions`, undefined, "GET"); } catch (e) { err = e; }
  const box = find();
  if (!box || !box.isConnected) return;
  if (err) { box.innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
  if (box.dataset.asset !== asset) box.dataset.sel = "[]";
  box.dataset.asset = asset;
  const sel = new Set(JSON.parse(box.dataset.sel || "[]").filter(n => d.versions.some(v => v.n === n)));
  const draw = () => {
    box.dataset.sel = JSON.stringify([...sel]);
    box.className = "version-strip";
    box.innerHTML = `<legend>Versions <span class="hint" style="margin:0" data-role="total">${d.versions.length} kept · ${fmtBytes(d.bytes)} · keeping the last ${d.keep} unpinned</span></legend>
      ${d.prunable.length ? `<div class="ver-prune row"><span>${d.prunable.length} old version${d.prunable.length === 1 ? "" : "s"} can be removed (${fmtBytes(d.prunable_bytes)})</span>
        <button type="button" class="small ver-prune-btn">Remove</button></div>` : ""}
      <div class="ver-list">${d.versions.map(v => `<div class="ver${v.current ? " cur" : ""}" data-n="${v.n}">
        <label class="check ver-pick"><input type="checkbox" class="ver-sel" ${sel.has(v.n) ? "checked" : ""} aria-label="Select v${v.n} to compare"></label>
        ${v.thumb ? `<img loading="lazy" alt="Thumbnail of v${v.n}" src="${fileUrl(v.thumb)}">` : `<div class="ver-nothumb">no render</div>`}
        <div class="ver-meta"><b>v${v.n}</b> <span class="hint" style="margin:0">${esc(v.kind)} · ${esc(verDate(v.created))}</span>
          ${v.current ? `<span class="mark m-rdy">current</span>` : ""}${v.approved ? `<span class="mark m-ok">approved</span>` : ""}${v.pinned ? `<span class="mark m-new">pinned</span>` : ""}
          <div class="hint ver-src" style="margin:0">${esc(verSource(v)) || "&nbsp;"}</div>
          <div class="hint" style="margin:0">${fmtBytes(v.bytes)}${v.has_glb && v.kind === "procedural" ? " · with 3D model" : ""}</div></div>
        <div class="ver-btns"><button type="button" class="small ver-restore"${v.current ? " disabled title=\"This is the current build\"" : ""}>Restore</button>
          <button type="button" class="small ver-pin">${v.pinned ? "Unpin" : "Pin"}</button>
          <button type="button" class="small ver-delete"${v.approved ? " disabled title=\"The Approved version can't be deleted\"" : ""}>Delete</button></div></div>`).join("")}</div>
      <div class="row"><button type="button" class="small ver-compare" ${sel.size === 2 ? "" : "disabled"}>Compare</button>
        <span class="hint" style="margin:0">Tick two versions to compare them.</span></div>`;
    box.querySelectorAll(".ver").forEach(row => {
      const n = +row.dataset.n, v = d.versions.find(x => x.n === n);
      $(".ver-sel", row).onchange = ev => {
        if (ev.target.checked) { if (sel.size >= 2) sel.delete([...sel][0]); sel.add(n); } else sel.delete(n);
        draw();
      };
      $(".ver-restore", row).onclick = () => versionRestore(asset, v, onChange);
      $(".ver-pin", row).onclick = async () => {
        try { await api(`/api/projects/${slug}/library/asset/${asset}/versions/${n}/${v.pinned ? "unpin" : "pin"}`, {}); }
        catch (err) { alert(errorText(err)); }
        versionStrip(box, asset, onChange);
      };
      $(".ver-delete", row).onclick = () => versionDelete(asset, v, () => versionStrip(box, asset, onChange));
    });
    const cmp = $(".ver-compare", box);
    if (cmp) cmp.onclick = () => versionCompare(asset, ...[...sel].sort((a, b) => a - b));
    const pr = $(".ver-prune-btn", box);
    if (pr) pr.onclick = async () => {
      const ok = await confirmDialog({title: "Remove old versions", button: "Remove", danger: true, id: "version-prune-dialog",
        body: `<p>Remove ${d.prunable.length} old version${d.prunable.length === 1 ? "" : "s"} of <b>${esc(asset)}</b>
          (${d.prunable.map(n => "v" + n).join(", ")}), freeing ${fmtBytes(d.prunable_bytes)}?</p>
          <p class="hint">The last ${d.keep} unpinned, the pinned, the Approved and the current version stay.</p>`});
      if (!ok) return;
      try { await api(`/api/projects/${slug}/library/asset/${asset}/versions/prune`, {dry_run: false}); }
      catch (err) { alert(errorText(err)); }
      versionStrip(box, asset, onChange);
    };
  };
  draw();
}

async function versionRestore(asset, v, onChange) {
  const body = v.kind === "trellis"
    ? `<p>Make v${v.n} (${esc(verSource(v))}) the 3D model of <b>${esc(asset)}</b> that cleanup and export use. Nothing is deleted.</p>`
    : `<p>Put the brief of v${v.n} back for <b>${esc(asset)}</b>${v.has_glb ? " and its saved 3D model as the build." : " and rebuild it (a new version)."}
       Unsaved edits to the brief are lost.</p>`;
  if (!await confirmDialog({title: `Restore v${v.n}`, button: "Restore", id: "version-restore-dialog", body})) return;
  try { const job = await api(`/api/projects/${slug}/library/asset/${asset}/versions/${v.n}/restore`, {}); await versionJob(job); }
  catch (err) { alert(errorText(err)); }
  if (onChange) await onChange();
}

async function versionDelete(asset, v, after) {
  const url = `/api/projects/${slug}/library/asset/${asset}/versions/${v.n}`;
  const ok = await confirmDialog({title: `Delete v${v.n}`, button: "Delete", danger: true, id: "version-delete-dialog",
    body: `<p>Delete v${v.n} of <b>${esc(asset)}</b> (${fmtBytes(v.bytes)})?${v.kind === "trellis" ? " Its 3D model file is removed too." : ""}</p>
      ${v.pinned ? `<p class="error">It is pinned. Deleting it removes the pin and its saved files.</p>` : ""}`});
  if (!ok) return;
  try { await api(url + (v.pinned ? "?confirm_pinned=true" : ""), {}, "DELETE"); }
  catch (err) { alert(errorText(err)); }
  after();
}

async function versionCompare(asset, a, b) {
  let c;
  try { c = await api(`/api/projects/${slug}/library/asset/${asset}/versions/compare?a=${a}&b=${b}`, undefined, "GET"); }
  catch (err) { alert(errorText(err)); return; }
  const val = x => x == null || x === "" ? "–" : typeof x === "object" ? JSON.stringify(x) : String(x);
  const col = (v, key) => `<figure class="ver-cmp-col" data-side="${key}"><b>v${v.n}</b> <span class="hint" style="margin:0">${esc(v.kind)} · ${esc(verDate(v.created))} · ${fmtBytes(v.bytes)}</span>
    ${v.thumb ? `<img alt="Thumbnail of v${v.n}" src="${fileUrl(v.thumb)}">` : `<div class="ver-nothumb">no render</div>`}
    <div class="hint">${esc(verSource(v))}</div></figure>`;
  const table = (rows, k1, k2, head) => rows.length ? `<table class="grid-table"><thead><tr><th>${head}</th><th>v${c.a.n}</th><th>v${c.b.n}</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r[k1])}</td><td>${esc(val(r.a))}</td><td>${esc(val(r.b))}</td></tr>`).join("")}</tbody></table>` : "";
  const body = document.createElement("div"); body.id = "version-compare-body";
  body.innerHTML = `<div class="ver-cmp">${col(c.a, "a")}${col(c.b, "b")}</div>
    <h3>Source</h3>${table(c.source_diff, "field", "", "Field") || `<p class="hint">Same source.</p>`}
    ${c.a.kind === "procedural" && c.b.kind === "procedural" ? `<h3>Brief</h3>${table(c.brief_diff, "path", "", "Value") || `<p class="hint">Same brief.</p>`}` : ""}`;
  const dlg = dialog({title: `Compare v${a} and v${b}`, body, className: "tool-window", buttons: [{label: "Close", default: true}]});
  dlg.win.id = "version-compare-dialog";
}

// --- Tools › Storage… -------------------------------------------------------------------
async function storageDialog() {
  if (noProject()) return;
  const body = document.createElement("div"); body.id = "storage-body";
  const dlg = dialog({title: "Storage", body, className: "tool-window", buttons: [{label: "Close", default: true}]});
  dlg.win.id = "storage-dialog";
  const draw = async () => {
    let s;
    try { s = await api(`/api/projects/${slug}/storage`, undefined, "GET"); }
    catch (err) { body.innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
    body.innerHTML = `<p id="st-total"><b>${fmtBytes(s.total)}</b> in asset builds and versions: ${fmtBytes(s.current_bytes)} current build files,
        ${fmtBytes(s.versions_bytes)} in versions. The last ${s.keep} unpinned versions of each asset are kept (Tools › Settings…).</p>
      ${s.assets.length ? `<table class="grid-table"><thead><tr><th>Asset</th><th>Versions</th><th>Versions size</th><th>Build files</th><th>Total</th><th></th></tr></thead><tbody>
        ${s.assets.map(r => `<tr data-asset="${esc(r.asset)}"><td>${esc(r.asset)}</td><td>${r.versions}${r.pinned ? ` (${r.pinned} pinned)` : ""}</td>
          <td>${fmtBytes(r.versions_bytes)}</td><td>${fmtBytes(r.current_bytes)}</td><td>${fmtBytes(r.total)}</td>
          <td><button type="button" class="small st-prune-asset"${r.removable ? "" : " disabled"}>Delete unpinned versions (${r.removable}, ${fmtBytes(r.removable_bytes)})</button></td></tr>`).join("")}
        </tbody></table>` : `<p class="hint">No asset builds yet.</p>`}
      <div class="row"><button type="button" id="st-prune-all"${s.removable ? "" : " disabled"}>Delete unpinned versions in the project (${s.removable}, ${fmtBytes(s.removable_bytes)})</button>
        <span class="hint" style="margin:0">The Approved, pinned and current versions are never deleted.</span></div>`;
    const prune = async (asset, n, bytes) => {
      const ok = await confirmDialog({title: "Delete unpinned versions", button: "Delete", danger: true, id: "storage-confirm-dialog",
        body: `<p>Delete ${n} unpinned version${n === 1 ? "" : "s"} ${asset ? `of <b>${esc(asset)}</b>` : "in the whole project"}, freeing ${fmtBytes(bytes)}?</p>
          <p class="hint">The Approved, pinned and current versions stay. This cannot be undone.</p>`});
      if (!ok) return;
      try { await api(`/api/projects/${slug}/storage/prune`, asset ? {asset} : {}); }
      catch (err) { alert(errorText(err)); }
      draw();
    };
    body.querySelectorAll("tr[data-asset]").forEach(tr => {
      const r = s.assets.find(x => x.asset === tr.dataset.asset);
      $(".st-prune-asset", tr).onclick = () => prune(r.asset, r.removable, r.removable_bytes);
    });
    $("#st-prune-all", body).onclick = () => prune(null, s.removable, s.removable_bytes);
  };
  draw();
}

// --- Tools › Settings… -------------------------------------------------------------------
async function settingsDialog() {
  if (noProject()) return;
  let s;
  try { s = await api(`/api/projects/${slug}/settings`, undefined, "GET"); }
  catch (err) { alert(errorText(err)); return; }
  const body = document.createElement("div"); body.id = "settings-body";
  body.innerHTML = `<div class="form"><label for="set-keep">Versions to keep:</label>
      <input id="set-keep" type="number" min="1" step="1" value="${s.versions_keep}" style="width:5em"></div>
    <p class="hint">Every build of an asset is kept as a version. After a build, versions beyond the last N unpinned ones are offered for
      removal in the asset's version strip (nothing is deleted without asking). Pinned, Approved and current versions are never removed.
      Set per project (default ${s.default}).</p><p class="error" id="set-err"></p>`;
  const dlg = dialog({title: "Settings", body, className: "tool-window", buttons: [
    {label: "Save", default: true, onClick: async () => {
      const v = Number($("#set-keep", body).value);
      if (!Number.isInteger(v) || v < 1) { $("#set-err", body).textContent = "Versions to keep must be a whole number, 1 or more."; return false; }
      try { await api(`/api/projects/${slug}/settings`, {versions_keep: v}, "PUT"); }
      catch (err) { $("#set-err", body).textContent = errorText(err); return false; }
    }}, {label: "Cancel"}]});
  dlg.win.id = "settings-dialog";
}

Object.assign(ACTIONS, {storage: {run: storageDialog, enabled: () => !!slug}, settings: {run: settingsDialog, enabled: () => !!slug}});
{
  const tools = MENUS.find(m => m[0] === "Tools")[2];
  tools.splice(tools.length, 0, "-", ["Storage…", "storage"], ["Settings…", "settings"]);
}
