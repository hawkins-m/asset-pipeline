// Library, World and Unreal menus: the runbook / USAGE commands that had no button. Each item
// opens a small dialog (confirming anything destructive) and posts to tools_api.py, which calls
// the same Python function as the CLI command. Needs shell.js, tools.js (helpers) and areas.js.
const P = path => `/api/projects/${slug}/${path}`;

// Start a job, wait for it, and show its result (or its error) in a dialog.
async function runTool(path, body, title, {show = true, id = "tool-result"} = {}) {
  let j;
  try { j = await api(P(path), body); await load(); j = await jobDone(j); }
  catch (err) { alert(errorText(err)); return null; }
  await load();
  if (j.status === "error") { alert(errorText(j.error)); return null; }
  if (j.status === "canceled") return null;
  if (show) textDialog(title, typeof j.result === "string" ? j.result : JSON.stringify(j.result, null, 2), id);
  return j;
}

const shotIds = () => ((siteData && siteData.shots) || []).map(s => s.id);
const optionList = (items, sel) => items.map(x => `<option${x === sel ? " selected" : ""}>${esc(x)}</option>`).join("");
const needShots = () => { if (noProject()) return true; if (!shotIds().length) { alert("No shots yet: build the greybox in the City tab first."); return true; } return false; };

function formDialog({title, id, html, buttons}) {
  const form = document.createElement("form"); form.className = "form"; form.innerHTML = html;
  form.onsubmit = e => e.preventDefault();
  const d = dialog({title, body: form, className: "tool-window", buttons});
  d.win.id = id;
  return {d, form};
}

// --- Library ----------------------------------------------------------------------------------
async function batchPicker(kind) {
  if (noProject()) return;
  let a;
  try { a = await api(P("areas"), undefined, "GET"); } catch (err) { alert(errorText(err)); return; }
  const opts = [...Object.keys(a.areas).map(n => ["a:" + n, `area ${n}`]), ...a.districts.map(x => ["d:" + x, `district ${x}`])];
  if (!opts.length) { alert("No area or district yet: define an area in Library › Areas…"); return; }
  const {d, form} = formDialog({title: `${AREA_BATCHES[kind][0]}: pick an area`, id: "batch-picker",
    html: `<label for="bp-sel">Area or district:</label><select id="bp-sel">${opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>`,
    buttons: [{label: "Next…", default: true, onClick: () => {
      areaState.sel = $("#bp-sel", form).value; setTimeout(() => areaBatch(kind, null, {close() {}}, () => {}), 0); }}, {label: "Cancel"}]});
}

function pendingDialog() {
  if (needShots()) return;
  const {d, form} = formDialog({title: "Pending slots", id: "pending-dialog",
    html: `<label for="pd-shot">Shot:</label><select id="pd-shot">${optionList(shotIds())}</select>
      <label class="check" style="grid-column:1/-1"><input type="checkbox" id="pd-nodraft"> No drafted briefs</label>
      <p class="hint" style="grid-column:1/-1">Slots the shot shows without an approved build, by frame share.</p>`,
    buttons: [{label: "Run", default: true, onClick: async () => {
      const j = await runTool("library/pending", {shot: $("#pd-shot", form).value, no_draft: $("#pd-nodraft", form).checked}, "", {show: false});
      if (j) textDialog("Pending slots", (j.result || []).map(r => `${(r.frame_share * 100).toFixed(2).padStart(6)}%  ${r.slot}: ${(r.state || []).join("; ")}`).join("\n") || "None.", "pending-result");
    }}, {label: "Close"}]});
}

function silhouetteDialog() {
  if (needShots()) return;
  formDialog({title: "Silhouette check", id: "silhouette-dialog",
    html: `<label for="sc-shot">Shot:</label><select id="sc-shot">${optionList(shotIds())}</select>
      <label for="sc-entries">Entries:</label><input id="sc-entries" placeholder="entry ids, separated by spaces">
      <p class="hint" style="grid-column:1/-1">Builds against their building's mask in the shot's locked frame (IoU, aspect).</p>`,
    buttons: [{label: "Run", default: true, onClick: async () => {
      const f = $("#silhouette-dialog form");
      await runTool("library/silhouette", {shot: $("#sc-shot", f).value, entries: $("#sc-entries", f).value.split(/[\s,]+/).filter(Boolean)}, "Silhouette check", {id: "silhouette-result"});
    }}, {label: "Close"}]});
}

async function reviewTasksDialog() {
  if (noProject()) return;
  try { const r = await api(P("library/review/tasks"), undefined, "GET"); textDialog(`Review round ${r.round}: task list`, r.text, "review-tasks-dialog"); }
  catch (err) { alert(errorText(err)); }
}

function reviewDoneDialog() {
  if (noProject()) return;
  formDialog({title: "Mark Ready for review", id: "review-done-dialog",
    html: `<label for="rd-assets">Assets:</label><input id="rd-assets" placeholder="assets you changed, separated by spaces">
      <p class="hint" style="grid-column:1/-1">Marks them Ready for review and opens the next round.</p>`,
    buttons: [{label: "Mark done", default: true, onClick: async () => {
      const f = $("#review-done-dialog form");
      try { const r = await api(P("library/review/done"), {assets: $("#rd-assets", f).value.split(/[\s,]+/).filter(Boolean)}); alert(`Round ${r.round} is open.`); }
      catch (err) { alert(errorText(err)); return false; }
    }}, {label: "Cancel"}]});
}

// --- World: frames, shots, site, terrain -------------------------------------------------------
function framesGenerateDialog() {
  if (needShots()) return;
  const locked = new Set(((framesData && framesData.shots) || []).filter(s => s.locked).map(s => s.id));
  const {d, form} = formDialog({title: "Generate frames", id: "frames-generate-dialog",
    html: `<fieldset style="grid-column:1/-1"><legend>Shots</legend>${shotIds().map(s => `<label class="check"><input type="checkbox" name="gf-shot" value="${esc(s)}"
      ${locked.has(s) ? "disabled" : ""}> ${esc(s)}${locked.has(s) ? " (locked)" : ""}</label>`).join(" ")}</fieldset>
      <label for="gf-n">Frames per shot:</label><input id="gf-n" type="number" min="1" max="12" value="${typeof framesPerShot === "function" ? framesPerShot() : 4}">
      <p class="hint" style="grid-column:1/-1">Needs ComfyUI and fresh shot passes. Adds a batch; earlier ones are kept.</p>`,
    buttons: [{label: "Generate", default: true, onClick: async () => {
      const picked = [...form.querySelectorAll("[name=gf-shot]:checked")].map(x => x.value);
      if (!picked.length) { alert("Pick at least one shot."); return false; }
      try { for (const s of picked) await api(P("frames/generate"), {shot: s, n: +$("#gf-n", form).value}); await load(); }
      catch (err) { alert(errorText(err)); return false; }
    }}, {label: "Cancel"}]});
}

function frameLockDialog() {
  if (!framesData || !framesData.shots.length) { alert("No shots with frames yet."); return; }
  const shots = framesData.shots;
  const frames = id => { const s = shots.find(x => x.id === id); return s ? s.batches.flatMap(b => b.frames) : []; };
  const {d, form} = formDialog({title: "Lock a shot", id: "frames-lock-dialog",
    html: `<label for="fl-shot">Shot:</label><select id="fl-shot">${optionList(shots.map(s => s.id))}</select>
      <label for="fl-frame">Frame:</label><select id="fl-frame"></select>
      <p class="hint" id="fl-state" style="grid-column:1/-1"></p>
      <p class="hint" style="grid-column:1/-1">A locked shot is never regenerated or re-placed.</p>`,
    buttons: [
      {label: "Lock", default: true, onClick: () => doLock({frame: $("#fl-frame", form).value})},
      {label: "Lock on greybox", onClick: () => doLock({greybox: true})},
      {label: "Unlock", onClick: () => doLock({unlock: true})}, {label: "Close"}]});
  const sync = () => {
    const id = $("#fl-shot", form).value, s = shots.find(x => x.id === id), fs = frames(id);
    $("#fl-frame", form).innerHTML = fs.map(f => `<option value="${esc(f.key)}">${esc(f.key.split("/").slice(-2).join("/"))}${data && data.stars[f.key] ? " ★" : ""}</option>`).join("");
    $("#fl-state", form).textContent = s.locked ? `Locked on ${s.locked}.` : "Not locked.";
  };
  const doLock = async extra => {
    try { await api(P("frames/lock"), {shot: $("#fl-shot", form).value, ...extra}); await load(); sync(); }
    catch (err) { alert(errorText(err)); }
    return false;
  };
  $("#fl-shot", form).onchange = sync; sync();
}

async function shotsDialog() {
  if (noProject()) return;
  let list;
  try { list = (await api(P("shots"), undefined, "GET")).shots; } catch (err) { alert(errorText(err)); return; }
  const body = document.createElement("div");
  body.innerHTML = `<table class="grid-table" id="shots-table"><thead><tr><th></th><th>Shot</th><th>Tier</th><th>Lens</th><th>State</th></tr></thead><tbody>
    ${list.map(r => `<tr><td><input type="checkbox" name="sh-pick" value="${esc(r.id)}" aria-label="Render ${esc(r.id)}"></td><td>${esc(r.id)}</td><td>${esc(r.tier)}</td>
      <td>${r.lens_mm} mm</td><td>${!r.rendered ? "not rendered" : r.stale ? "STALE" : "rendered"}${r.warnings && r.warnings.length ? ` · ${esc(r.warnings.join("; "))}` : ""}</td></tr>`).join("")
      || `<tr><td colspan="5">No shots (build the greybox first).</td></tr>`}</tbody></table>`;
  const d = dialog({title: "Shots", body, className: "tool-window", buttons: [
    {label: "Render passes for checked", default: true, onClick: async () => {
      const picked = [...body.querySelectorAll("[name=sh-pick]:checked")].map(x => x.value);
      if (!picked.length) { alert("Check at least one shot."); return false; }
      try { await api(P("shots/render"), {shots: picked}); await load(); } catch (err) { alert(errorText(err)); return false; }
    }},
    {label: "Add shot…", onClick: () => { setTimeout(addShotDialog, 0); }}, {label: "Close"}]});
  d.win.id = "shots-dialog";
}

function addShotDialog() {
  if (noProject()) return;
  const {d, form} = formDialog({title: "Add shot", id: "add-shot-dialog",
    html: `<label for="as-id">Id:</label><input id="as-id" placeholder="street-2" required>
      <label for="as-pos">Position (x,y,z m):</label><input id="as-pos" placeholder="0,-120,1.6">
      <label for="as-look">Look at (x,y,z m):</label><input id="as-look" placeholder="0,0,12">
      <label for="as-lens">Lens (mm):</label><input id="as-lens" type="number" value="35">
      <label for="as-tier">Tier:</label><select id="as-tier"><option>wide</option><option selected>medium</option><option>tight</option></select>
      <label for="as-res">Resolution:</label><input id="as-res" value="1344x768">
      <label for="as-district">District:</label><input id="as-district" placeholder="optional">
      <label for="as-notes">Notes:</label><input id="as-notes" placeholder="optional">`,
    buttons: [{label: "Add", default: true, onClick: async () => {
      const vec = id => { const v = $(id, form).value.split(",").map(Number); return v.length === 3 && v.every(Number.isFinite) ? v : null; };
      const pos = vec("#as-pos"), look = vec("#as-look");
      if (!$("#as-id", form).value.trim() || !pos || !look) { alert("Give an id, a position and a look-at point as x,y,z."); return false; }
      try {
        const j = await api(P("shots/add"), {id: $("#as-id", form).value.trim(), pos, look_at: look, lens: +$("#as-lens", form).value,
          tier: $("#as-tier", form).value, res: $("#as-res", form).value, district: $("#as-district", form).value || null, notes: $("#as-notes", form).value});
        await load(); await jobDone(j); await load();
      } catch (err) { alert(errorText(err)); return false; }
    }}, {label: "Cancel"}]});
}

function vegetationDialog() {
  if (noProject()) return;
  const shots = shotIds();
  formDialog({title: "Vegetation slots", id: "vegetation-dialog",
    html: `<p class="hint" style="grid-column:1/-1">Street-tree slots on streets and plazas (data for Unreal PCG); the city plan is unchanged.</p>
      <label for="vg-shot">Preview shot:</label><select id="vg-shot"><option value="">(none)</option>${optionList(shots)}</select>`,
    buttons: [{label: "Run", default: true, onClick: async () => {
      const f = $("#vegetation-dialog form"), s = $("#vg-shot", f).value;
      await runTool("site/vegetation", {preview: s ? [s] : []}, "Vegetation slots", {id: "vegetation-result"});
    }}, {label: "Close"}]});
}

function overlapDialog() {
  if (needShots()) return;
  formDialog({title: "Plan overlap", id: "overlap-dialog",
    html: `<label for="ov-shot">Shot:</label><select id="ov-shot">${optionList(shotIds())}</select>
      <p class="hint" style="grid-column:1/-1">Ground classes of the greybox against a locked frame's plan (needs site/reconcile/SHOT).</p>`,
    buttons: [{label: "Run", default: true, onClick: async () => {
      await runTool("site/overlap", {shot: $("#ov-shot", $("#overlap-dialog form")).value}, "Plan overlap", {id: "overlap-result"});
    }}, {label: "Close"}]});
}

const TERRAIN = {
  analyse: ["Analyse locked skylines", "Reads the locked shots' skylines into a terrain proposal."],
  build: ["Build terrain", "Builds the ranges from site/terrain_plan/proposal.json, fits the sky and erodes."],
  outer: ["Rebuild outer ring", "Only the outer ring and its GLB, from the far build."],
  apply: ["Apply to layout…", "Points the layout at the built terrain. The city plan can change: check it afterwards.", true],
  landscape: ["Unreal landscape tiles", "Landscape tiles and the outer ring GLB for Unreal."],
  ue: ["Terrain into the Unreal project…", "Auto material and outer ring in the Unreal project.", true],
};
async function terrainAction(action) {
  if (noProject()) return;
  const [title, note, confirmFirst] = TERRAIN[action];
  if (confirmFirst && !(await confirmDialog({title, button: title.replace("…", ""), danger: true, id: "terrain-confirm", body: `<p>${esc(note)}</p>`}))) return;
  await runTool(`terrain/${action}`, {confirm: !!confirmFirst}, `Terrain: ${title.replace("…", "")}`, {id: "terrain-result"});
}

// --- Unreal -------------------------------------------------------------------------------------
async function ueImportDialog() {
  if (noProject()) return;
  let u;
  try { u = await api(P("unreal"), undefined, "GET"); } catch (err) { alert(errorText(err)); return; }
  const backups = u.backups.length;
  const {d, form} = formDialog({title: "Unreal import", id: "ue-import-dialog",
    html: `<p class="hint" style="grid-column:1/-1">Imports export/manifest.json into the UE level. Runs the editor headless.
        ${u.editor_open ? `<span class="error">An Unreal Editor has the project open: close it first.</span>` : ""}</p>
      <label class="check" style="grid-column:1/-1"><input type="checkbox" id="ui-reset"> Reset layout: move every actor back to the manifest (discards layout edits made in UE)</label>
      <label class="check" style="grid-column:1/-1"><input type="checkbox" id="ui-stale" disabled> Only stale slots (keeps slots you moved by hand)</label>
      <label class="check" style="grid-column:1/-1"><input type="checkbox" id="ui-prune"> Prune: delete pipeline actors that are not in the manifest</label>
      <label for="ui-expect">Expect N pruned:</label><input id="ui-expect" type="number" min="0" placeholder="needed above 10 actors">
      <label class="check" style="grid-column:1/-1"><input type="checkbox" id="ui-veg"> Also place vegetation points (untested in Unreal)</label>
      <p class="hint error" id="ui-backup-warn" style="grid-column:1/-1" hidden>Reset and prune need a backup first: ${backups ? "" : "there is none yet."}
        <button type="button" class="small" id="ui-backup">Back up now</button></p>
      <p class="hint" id="ui-backups" style="grid-column:1/-1">${backups} backup snapshot${backups === 1 ? "" : "s"}${backups ? `, newest ${esc(u.backups.at(-1))}` : ""}.</p>`,
    buttons: [{label: "Import", default: true, onClick: () => doImport()}, {label: "Stale report…", onClick: () => { setTimeout(ueStaleDialog, 0); return false; }}, {label: "Close"}]});
  const q = id => $(id, form), destructive = () => q("#ui-reset").checked || q("#ui-prune").checked;
  const sync = () => {
    q("#ui-stale").disabled = !q("#ui-reset").checked; if (!q("#ui-reset").checked) q("#ui-stale").checked = false;
    q("#ui-backup-warn").hidden = !(destructive() && !backups);
    $(".wbuttons .default", d.win).disabled = destructive() && !backups;
  };
  form.querySelectorAll("input").forEach(x => x.onchange = sync);
  q("#ui-backup").onclick = async () => { await ueBackup(); alert("Backup started: reopen this dialog when it is done."); };
  sync();
  const doImport = async () => {
    const body = {reset_layout: q("#ui-reset").checked, only_stale: q("#ui-stale").checked, prune: q("#ui-prune").checked,
      expect: q("#ui-expect").value === "" ? null : +q("#ui-expect").value, vegetation: q("#ui-veg").checked};
    if (destructive()) {
      const what = [body.reset_layout ? "move every actor back to the manifest" + (body.only_stale ? " (stale slots only)" : "") : "",
        body.prune ? "delete pipeline actors that are not in the manifest" : ""].filter(Boolean).join(" and ");
      if (!(await confirmDialog({title: "Destructive import", button: "Import and discard", danger: true, id: "ue-import-confirm",
        body: `<p>This will ${esc(what)}. Layout work done in Unreal is lost for those actors; the latest backup is ${esc(u.backups.at(-1))}.</p>`}))) return false;
    }
    setTimeout(() => runTool("ue/import", body, "Unreal import", {id: "ue-import-result"}), 0);
  };
}

async function ueStaleDialog() {
  if (noProject()) return;
  try {
    const r = await api(P("ue/stale"), undefined, "GET");
    textDialog("Stale slots", `${r.current} slots where the current greybox puts them; ${r.stale.length} stale (at an earlier committed greybox's place); ${r.hand.length} moved by hand.\n` +
      (r.hand.length ? `\nMoved by hand (kept):\n${r.hand.join("\n")}` : ""), "ue-stale-dialog");
  } catch (err) { alert(errorText(err)); }
}

function ueRenderDialog() {
  if (needShots()) return;
  const {d, form} = formDialog({title: "Render shots in Unreal", id: "ue-render-dialog",
    html: `<fieldset style="grid-column:1/-1"><legend>Shots (none checked: all)</legend>${shotIds().map(s => `<label class="check"><input type="checkbox" name="ur-shot" value="${esc(s)}"> ${esc(s)}</label>`).join(" ")}</fieldset>
      <p class="hint" style="grid-column:1/-1">Runs the full editor offscreen on a GPU with little VRAM in use (it checks both). Shaders compile on the first run, which takes a while.</p>`,
    buttons: [{label: "Render", default: true, onClick: async () => {
      await runTool("ue/render", {shots: [...form.querySelectorAll("[name=ur-shot]:checked")].map(x => x.value)}, "Unreal renders", {id: "ue-render-result"});
    }}, {label: "Cancel"}]});
}

async function ueLookDialog() {
  if (noProject()) return;
  let looks;
  try { looks = (await api(P("ue/looks"), undefined, "GET")).looks; } catch (err) { alert(errorText(err)); return; }
  if (!looks.length) { alert("No look presets in site/look_tests/ yet."); return; }
  formDialog({title: "Look scene", id: "ue-look-dialog",
    html: `<label for="ul-name">Preset:</label><select id="ul-name">${optionList(looks)}</select>
      <p class="hint" style="grid-column:1/-1">Rebuilds the preset's shot in a level of its own and renders it. Runs Unreal; takes minutes.</p>`,
    buttons: [{label: "Render", default: true, onClick: async () => {
      await runTool("ue/look", {name: $("#ul-name", $("#ue-look-dialog form")).value}, "Look scene", {id: "ue-look-result"});
    }}, {label: "Cancel"}]});
}

const quick = (path, title, id) => () => noProject() || runTool(path, {}, title, {id});
Object.assign(ACTIONS, {
  batchSheets: {run: () => batchPicker("sheets"), enabled: () => !!slug}, batchBuild: {run: () => batchPicker("build"), enabled: () => !!slug},
  batchTrellis: {run: () => batchPicker("trellis"), enabled: () => !!slug}, batchInstances: {run: () => batchPicker("instances"), enabled: () => !!slug},
  pending: {run: pendingDialog, enabled: () => !!slug}, silhouette: {run: silhouetteDialog, enabled: () => !!slug},
  reviewTasks: {run: reviewTasksDialog, enabled: () => !!slug}, reviewDone: {run: reviewDoneDialog, enabled: () => !!slug},
  framesGenerate: {run: framesGenerateDialog, enabled: () => !!slug}, framesLock: {run: frameLockDialog, enabled: () => !!slug},
  shotsDialog: {run: shotsDialog, enabled: () => !!slug}, addShot: {run: addShotDialog, enabled: () => !!slug},
  vegetation: {run: vegetationDialog, enabled: () => !!slug}, overlap: {run: overlapDialog, enabled: () => !!slug},
  ueImport: {run: ueImportDialog, enabled: () => !!slug}, ueStale: {run: ueStaleDialog, enabled: () => !!slug},
  ueRender: {run: ueRenderDialog, enabled: () => !!slug}, uePull: {run: quick("ue/pull-layout", "Pull layout from Unreal", "ue-pull-result"), enabled: () => !!slug},
  ueLook: {run: ueLookDialog, enabled: () => !!slug}, ueInit: {run: quick("ue/init", "Unreal project", "ue-init-result"), enabled: () => !!slug},
});
for (const k of Object.keys(TERRAIN)) ACTIONS["terrain_" + k] = {run: () => terrainAction(k), enabled: () => !!slug};

{
  const at = MENUS.findIndex(m => m[0] === "Tools");
  MENUS.splice(at, 0,
    ["Library", "L", [["Discover assets from locked shots", "discoverAssets"], ["Areas…", "areas"], "-", ["Sheets for missing…", "batchSheets"], ["Build all…", "batchBuild"], ["TRELLIS all…", "batchTrellis"],
      ["Instances…", "batchInstances"], "-", ["Pending slots…", "pending"], ["Silhouette check…", "silhouette"], "-",
      ["Review round: task list…", "reviewTasks"], ["Review round: mark done…", "reviewDone"]]],
    ["World", "W", [["Generate frames…", "framesGenerate"], ["Lock a shot…", "framesLock"], "-", ["Shots…", "shotsDialog"], ["Add shot…", "addShot"], "-",
      ["Vegetation slots…", "vegetation"], ["Plan overlap…", "overlap"], "-",
      ...Object.entries(TERRAIN).map(([k, v]) => ["Terrain: " + v[0], "terrain_" + k])]],
    ["Unreal", "U", [["Create UE project", "ueInit"], ["Import…", "ueImport"], ["Stale report…", "ueStale"], "-", ["Render shots…", "ueRender"],
      ["Look scene…", "ueLook"], ["Pull layout", "uePull"]]]);
  buildMenus();
}
