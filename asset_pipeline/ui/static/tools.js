// Tools and Help menu items for the commands that had no button: Preflight, Vision LLM up/down,
// the desktop launcher and the demo. Also the small helpers world.js and areas.js share
// (job results, confirm dialogs, copy to clipboard) and the extra menus' wiring. Loaded after
// shell.js; the server side is asset_pipeline/ui/tools_api.py.
const sleep = ms => new Promise(r => setTimeout(r, ms));

// The finished job (result or error) once it leaves the queue. api() has already opened its
// progress dialog; this only waits.
async function jobDone(job) {
  for (let i = 0; i < 20000; i++) {
    const j = await api(`/api/jobs/${job.id}`, undefined, "GET");
    if (!isActive(j)) return j;
    await sleep(800);
  }
  throw new Error("The job is still running; follow it in the Jobs panel.");
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* no clipboard permission */ }
  const t = document.createElement("textarea"); t.value = text; document.body.append(t); t.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  t.remove(); return ok;
}

// A dialog that must be answered with an explicit button (never window.confirm for destructive things).
function confirmDialog({title, body, button = "OK", danger = false, id = "confirm-dialog"}) {
  return new Promise(resolve => {
    let answered = false;
    const d = dialog({title, body, className: danger ? "danger-window" : "", onClose: () => { if (!answered) resolve(false); },
      buttons: [{label: button, default: !danger, onClick: () => { answered = true; resolve(true); }}, {label: "Cancel"}]});
    d.win.id = id;
    $(".wbuttons button", d.win).dataset.role = "confirm";
  });
}

function textDialog(title, text, id) {
  const pre = document.createElement("pre"); pre.className = "mono"; pre.style.cssText = "max-height:50vh;overflow:auto;margin:0;white-space:pre-wrap";
  pre.textContent = text;
  const d = dialog({title, body: pre, className: "tool-window", buttons: [
    {label: "Copy", onClick: async () => { await copyText(text); return false; }}, {label: "Close", default: true}]});
  d.win.id = id || "text-dialog";
  return d;
}

const noProject = () => { if (!slug) { alert("Open a project first."); return true; } return false; };

// --- Tools › Preflight… -------------------------------------------------------------------
function preflightDialog() {
  const body = document.createElement("div"); body.id = "preflight-body";
  body.innerHTML = `<label class="check"><input type="checkbox" id="pf-ue"> Also require Unreal Engine</label>
    <div id="pf-list" class="hint">Checking…</div>`;
  const d = dialog({title: "Preflight", body, className: "tool-window", buttons: [
    {label: "Run again", onClick: () => { run(); return false; }}, {label: "Close", default: true}]});
  d.win.id = "preflight-dialog";
  const run = async () => {
    const list = $("#pf-list", d.win); list.textContent = "Checking…";
    let r;
    try { r = await api("/api/preflight", {ue: $("#pf-ue", d.win).checked}); }
    catch (err) { list.innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
    list.className = "";
    list.innerHTML = `<table class="grid-table"><tbody>${r.checks.map((c, i) => `<tr data-check="${esc(c.name)}">
      <td class="${c.ok ? "ok" : c.required ? "error" : ""}">${c.ok ? "pass" : c.required ? "FAIL" : "warn"}</td>
      <td><b>${esc(c.name)}</b><br><span class="hint" style="margin:0">${esc(c.detail)}</span>
        ${c.fix && c.fix.kind === "install" ? `<br><code>${esc(c.fix.command)}</code> <button type="button" class="small pf-copy" data-i="${i}">Copy</button>
          <span class="hint" style="margin:0">See ${esc(c.fix.doc)}</span>` : ""}</td>
      <td>${c.fix && c.fix.kind === "start_comfy" ? `<button type="button" class="small pf-start">Start ComfyUI</button>` : ""}</td></tr>`).join("")}</tbody></table>
      <p><b>${r.ok ? "Ready: every required check passed." : "Not ready: fix the failed checks."}</b></p>`;
    list.querySelectorAll(".pf-copy").forEach(b => b.onclick = async () => {
      b.textContent = (await copyText(r.checks[+b.dataset.i].fix.command)) ? "Copied" : "Copy failed";
    });
    list.querySelectorAll(".pf-start").forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { const j = await api("/api/comfy/start", {}); await jobDone(j); } catch (err) { alert(errorText(err)); }
      run();
    });
  };
  $("#pf-ue", d.win).onchange = run;
  run();
}

// --- Tools › Vision LLM status…: Up / Down ------------------------------------------------
async function vlmControl(what, force = false) {
  try {
    const j = await api(what === "up" ? "/api/vlm/up" : "/api/vlm/down", what === "up" ? {} : {force});
    await jobDone(j);
  } catch (err) { alert(errorText(err)); }
  await load();
}

// --- Tools › Install desktop launcher… ------------------------------------------------------
async function launcherDialog() {
  let st;
  try { st = await api("/api/launcher", undefined, "GET"); } catch (err) { alert(errorText(err)); return; }
  const show = s => s.installed ? `Installed: ${s.desktop.path}` : "Not installed.";
  const d = dialog({title: "Desktop launcher", className: "tool-window", body: `<p id="launcher-state">${esc(show(st))}</p>
    <p class="hint">Adds a Terraformer entry to the applications menu. It opens this page, starting the UI first when it is not
    running. Files go to <code>~/.local/share/applications</code>, <code>~/.local/share/icons</code> and
    <code>~/.local/bin</code>. Same as <code>ap launcher install|remove</code>.</p>`, buttons: [
    {label: "Install", default: true, onClick: async () => {
      try { await api("/api/launcher/install", {}); $("#launcher-state", d.win).textContent = "Installed."; }
      catch (err) { alert(errorText(err)); }
      return false; }},
    {label: "Remove", onClick: async () => {
      try { await api("/api/launcher/remove", {}); $("#launcher-state", d.win).textContent = "Removed."; }
      catch (err) { alert(errorText(err)); }
      return false; }},
    {label: "Close"}]});
  d.win.id = "launcher-dialog";
}

// --- Help › Run demo… -------------------------------------------------------------------------
function demoDialog() {
  const shots = (siteData && siteData.shots || []).map(s => s.id);
  const body = document.createElement("form"); body.className = "form";
  body.innerHTML = `<label for="demo-mode">Demo:</label><select id="demo-mode">
      <option value="starter">Starter: new project, greybox, shot passes, two frames</option>
      <option value="area">Area: frames, builds, export, Unreal import and a look scene (needs Unreal)</option></select>
    <label for="demo-slug">Project:</label><input id="demo-slug" value="my-project" pattern="[a-z0-9](?:[a-z0-9_]|-)*">
    <label for="demo-shot" class="demo-area">Shot:</label><select id="demo-shot" class="demo-area">${shots.map(s => `<option>${esc(s)}</option>`).join("")}</select>
    <label for="demo-entries" class="demo-area">Entries:</label><input id="demo-entries" class="demo-area" placeholder="entry ids, separated by spaces">
    <p class="hint" style="grid-column:1/-1">Runs <code>scripts/demo.sh</code>; its output shows in the job. Run Preflight first.
      Starter needs ComfyUI; area also runs Unreal.</p>`;
  const sync = () => body.querySelectorAll(".demo-area").forEach(el => { el.hidden = $("#demo-mode", body).value !== "area"; });
  $("#demo-mode", body).onchange = sync; sync();
  const d = dialog({title: "Run demo", body, className: "tool-window", buttons: [
    {label: "Run", default: true, onClick: async () => {
      const mode = $("#demo-mode", body).value;
      const payload = {mode, slug: $("#demo-slug", body).value.trim() || "my-project"};
      if (mode === "area") { payload.shot = $("#demo-shot", body).value; payload.entries = $("#demo-entries", body).value.split(/[\s,]+/).filter(Boolean); }
      else if (!(await confirmDialog({title: "Run starter demo", button: "Run", id: "demo-confirm",
          body: `<p>Creates project <b>${esc(payload.slug)}</b> and generates images with ComfyUI (a few minutes).</p>`}))) return false;
      try { await api("/api/demo", payload); await load(); } catch (err) { alert(errorText(err)); return false; }
    }}, {label: "Cancel"}]});
  d.win.id = "demo-dialog";
}

// --- File › Commit project: the end-of-day commit of the project's own git repo ----------------
async function commitDialog() {
  const body = document.createElement("div"); body.className = "commit-dlg";
  body.innerHTML = "<p>Checking…</p>";
  const d = dialog({title: "Commit project", body, buttons: []});
  d.win.id = "commit-dialog";
  let g;
  try { g = await api(`/api/projects/${slug}/git`, undefined, "GET"); } catch (err) { body.innerHTML = `<p class="error">${esc(errorText(err))}</p>`; return; }
  if (!g.repo) { body.innerHTML = `<p>This project folder is not a git repository yet.</p>`; return; }
  body.innerHTML = `<p>${g.changes.length ? `${g.changes.length} changed file(s) in <code>${esc(slug)}</code>
      (the project's own private repo; its .gitignore keeps generated media out):` : "Nothing to commit."}</p>
    ${g.changes.length ? `<pre class="commit-list">${esc(g.changes.join("\n"))}</pre>
      <div class="form"><label for="commit-msg">Message:</label><input id="commit-msg" value="${esc(new Date().toISOString().slice(0, 10))}: area work"></div>
      <div class="row end"><button type="button" class="default" id="commit-go">Commit</button></div>` : ""}
    <p class="hint">Last commit: ${esc(g.last || "none")}</p>`;
  const go = $("#commit-go", body);
  if (go) go.onclick = async () => {
    go.disabled = true;
    try { const r = await api(`/api/projects/${slug}/git/commit`, {message: $("#commit-msg", body).value});
          body.innerHTML = `<p class="status-ok">Committed ${esc(r.committed)}.</p>`; }
    catch (err) { go.disabled = false; alert(errorText(err)); }
  };
}

// --- menu wiring ---------------------------------------------------------------------------------
Object.assign(ACTIONS, {
  preflight: {run: preflightDialog},
  launcher: {run: launcherDialog},
  commitProject: {run: commitDialog, enabled: () => !!slug},
  demo: {run: demoDialog},
  vlmUp: {run: () => vlmControl("up")},
  vlmDown: {run: () => confirmDialog({title: "Stop the vision LLM", button: "Stop it", id: "vlm-down-dialog",
    body: "<p>Stops the local vision LLM server. A request in flight finishes first. It starts again on demand.</p>"})
    .then(ok => ok && vlmControl("down"))},
});
{
  const tools = MENUS.find(m => m[0] === "Tools")[2];
  tools.splice(0, 0, ["Preflight…", "preflight"], "-");
  tools.splice(tools.findIndex(x => x[1] === "vlmStatus") + 1, 0, ["Start vision LLM", "vlmUp"], ["Stop vision LLM…", "vlmDown"]);
  tools.push("-", ["Install desktop launcher…", "launcher"]);
  MENUS.find(m => m[0] === "Help")[2].splice(0, 0, ["Run demo…", "demo"], "-");
  const file = MENUS.find(m => m[0] === "File")[2];
  file.splice(file.findIndex(x => x[1] === "refresh"), 0, ["Commit project…", "commitProject"], "-");
}

// job kinds started from these menus (names in the Jobs panel, titles in the progress dialog)
Object.assign(JOB_LABELS, {"library.sheets": "area sheets", "library.build": "area build", "library.trellis": "area TRELLIS",
  "library.instances": "area instances", "library.brief": "brief and build", "library.pending": "pending slots",
  "library.silhouette": "silhouette check", "shots.add": "adding a shot", "site.vegetation": "vegetation slots",
  "site.overlap": "plan overlap", "ue.init": "UE project", "ue.import": "Unreal import", "ue.render": "Unreal renders",
  "ue.pull": "pull layout", "ue.look": "look scene", "terrain.analyse": "terrain analysis", "terrain.build": "terrain build",
  "terrain.outer": "outer ring", "terrain.apply": "terrain apply", "terrain.landscape": "landscape tiles", "terrain.ue": "terrain in Unreal",
  "demo": "demo", "vlm.up": "starting the vision LLM", "vlm.down": "stopping the vision LLM"});
Object.assign(LOADERS, {"library.sheets": ["Drawing reference sheets…", "sheet", "units"], "library.build": ["Building assets…", "greybox", "assets"],
  "library.trellis": ["Building 3D models…", "model", "assets"], "library.instances": ["Building instances…", "greybox", "assets"],
  "library.brief": ["Brief and build…", "greybox", ""], "library.pending": ["Listing pending slots…", "plan", ""],
  "library.silhouette": ["Checking silhouettes…", "scene", ""], "shots.add": ["Adding the shot…", "passes", ""],
  "site.vegetation": ["Placing street trees…", "plan", ""], "site.overlap": ["Comparing ground classes…", "plan", ""],
  "ue.init": ["Creating the UE project…", "export", ""], "ue.import": ["Importing into Unreal…", "export", ""],
  "ue.render": ["Rendering in Unreal…", "passes", "shots"], "ue.pull": ["Reading the layout from Unreal…", "export", ""],
  "ue.look": ["Rendering the look scene…", "passes", ""], "terrain.analyse": ["Analysing skylines…", "scene", ""],
  "terrain.build": ["Building terrain…", "greybox", ""], "terrain.outer": ["Rebuilding the outer ring…", "greybox", ""],
  "terrain.apply": ["Applying terrain…", "greybox", ""], "terrain.landscape": ["Writing landscape tiles…", "export", ""],
  "terrain.ue": ["Terrain into Unreal…", "export", ""], "demo": ["Running the demo…", "frames", ""],
  "vlm.up": ["Starting the vision LLM…", "scene", ""], "vlm.down": ["Stopping the vision LLM…", "scene", ""]});
