// Window chrome: tabs, menu bar (dropdowns, mnemonics, shortcuts), toolbar, side panel
// (status, stages, jobs per lane), status bar and classic dialogs. Pages render their own
// content (app.js, site.js, edit.js, frames.js...); render() calls renderShell() last.
const TABS = ["style", "site", "city", "shots", "frames", "library", "views", "unreal"];
const TAB_NAMES = {style: "Style", site: "Site", city: "City", shots: "Shots", frames: "Frames", library: "Library",
  views: "Views and 3D", unreal: "Unreal"};
const LANES = [["gpu0", "GPU 0"], ["comfy", "GPU 1"], ["cpu", "CPU"]];
let currentTab = "style";
const saveHandlers = {};      // tab -> () => void: Edit › Save (Ctrl+S) on that tab
const revertHandlers = {};    // tab -> () => void: Edit › Revert
const statusLeft = {};        // tab -> () => text for the status bar's first cell ("" = the tab name)
const statusMid = {};         // tab -> () => text for the second cell (default: the plan's building count)
const statusRight = {};       // tab -> () => text for the right cell (default: ComfyUI's state)

// --- tabs ------------------------------------------------------------------------------
function showTab(tab) {
  if (!TABS.includes(tab)) tab = "style";
  currentTab = tab;
  document.querySelectorAll("#tabs [role=tab]").forEach(b => {
    const on = b.dataset.tab === tab;
    b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1;
  });
  document.querySelectorAll(".tabpanel[data-tab]").forEach(m => { m.hidden = m.dataset.tab !== tab; });
  $("#side-default").hidden = tab === "frames"; $("#side-frames").hidden = tab !== "frames";
  try { localStorage.setItem("ap.tab", tab); } catch (_) { /* private window */ }
  if (data) renderShell();
}
function tabKeys(list, attr, show) {   // arrow keys move between tabs, as in a desktop tab control
  list.addEventListener("keydown", e => {
    const tabs = [...list.querySelectorAll("[role=tab]")], i = tabs.indexOf(document.activeElement);
    if (i < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const j = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    show(tabs[j].dataset[attr]); tabs[j].focus();
  });
  list.addEventListener("click", e => { const b = e.target.closest("[role=tab]"); if (b) show(b.dataset[attr]); });
}
tabKeys($("#tabs"), "tab", showTab);

function showSub(sub) {
  document.querySelectorAll("#lib-tabs [role=tab]").forEach(b => b.setAttribute("aria-selected", b.dataset.sub === sub));
  document.querySelectorAll("#library-tab [data-sub]:not([role=tab])").forEach(el => { el.hidden = el.dataset.sub !== sub; });
  try { localStorage.setItem("ap.sub", sub); } catch (_) { /* ignore */ }
}
tabKeys($("#lib-tabs"), "sub", showSub);
{
  let t = URL_ARGS.get("tab"), sub = null;
  try { t = t || localStorage.getItem("ap.tab"); sub = localStorage.getItem("ap.sub"); } catch (_) { /* ignore */ }
  // links from the earlier UI: plan/refs live in Library now, review is Views and 3D
  if (t === "plan" || t === "refs") { sub = t; t = "library"; }
  if (t === "review") t = "views";
  showTab(t || "style");
  showSub(sub === "refs" ? "refs" : "plan");
}

// --- classic dialogs ---------------------------------------------------------------------
// dialog({title, body (node or html), buttons: [{label, default, onClick -> false keeps open}], onClose})
function dialog({title, body, buttons = [{label: "OK", default: true}], onClose, className = ""}) {
  const back = document.createElement("div"); back.className = "modal-back";
  const win = document.createElement("div"); win.className = `window ${className}`;
  win.setAttribute("role", "dialog"); win.setAttribute("aria-modal", "true"); win.setAttribute("aria-label", title);
  win.innerHTML = `<div class="wtitle"><span class="wt">${esc(title)}</span><button type="button" class="wclose" aria-label="Close">x</button></div>
    <div class="wbody"></div><div class="wbuttons"></div>`;
  const bodyEl = $(".wbody", win);
  if (typeof body === "string") bodyEl.innerHTML = body; else if (body) bodyEl.append(body);
  const prev = document.activeElement;
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey, true); onClose && onClose(); if (prev && prev.focus) prev.focus(); };
  for (const b of buttons) {
    const el = document.createElement("button"); el.type = "button"; el.textContent = b.label;
    if (b.default) el.className = "default";
    el.onclick = async () => { if (!b.onClick || (await b.onClick(win)) !== false) close(); };
    $(".wbuttons", win).append(el);
  }
  $(".wclose", win).onclick = close;
  const onKey = e => {
    if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); }
    if (e.key === "Tab") {   // keep focus in the dialog
      const f = [...win.querySelectorAll("button, input, select, textarea, a[href]")].filter(x => !x.disabled);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f.at(-1).focus(); }
      else if (!e.shiftKey && document.activeElement === f.at(-1)) { e.preventDefault(); f[0].focus(); }
    }
  };
  document.addEventListener("keydown", onKey, true);
  back.append(win); $("#dialogs").append(back);
  (win.querySelector("input, textarea, select") || win.querySelector(".wbuttons .default") || $(".wclose", win)).focus();
  return {win, close};
}

function newProjectDialog() {
  const f = document.createElement("form"); f.className = "form"; f.id = "new-form";
  f.innerHTML = `<label for="np-slug">Slug:</label><input id="np-slug" name="slug" placeholder="my-project" required pattern="[a-z0-9][a-z0-9_-]*">
    <label for="np-brief">Brief:</label><input id="np-brief" name="brief" placeholder="optional">`;
  const submit = async () => {
    if (!f.reportValidity()) return false;
    const fd = new FormData(f);
    try {
      await api("/api/projects", {slug: fd.get("slug"), brief: fd.get("brief")});
      try { localStorage.setItem("ap.project", fd.get("slug")); } catch (_) { /* ignore */ }
      await loadProjects(fd.get("slug"));
    } catch (err) { alert(err.message); return false; }
  };
  const d = dialog({title: "New project", body: f, buttons: [{label: "Create", default: true, onClick: submit}, {label: "Cancel"}]});
  f.onsubmit = async e => { e.preventDefault(); if ((await submit()) !== false) d.close(); };
}

// --- pipeline actions (menus, toolbar, shortcuts) ------------------------------------------
const allActive = () => (data ? data.jobs : []).filter(isActive);
const isCity = () => !!(catalogData && catalogData.city);
const ACTIONS = {
  newProject: {run: newProjectDialog},
  openProject: {run: () => $("#project").focus()},
  refresh: {run: () => load()},
  save: {run: () => saveHandlers[currentTab] && saveHandlers[currentTab](), enabled: () => !!saveHandlers[currentTab]},
  revert: {run: () => revertHandlers[currentTab] && revertHandlers[currentTab](), enabled: () => !!revertHandlers[currentTab]},
  find: {run: () => { const f = $(`.tabpanel[data-tab="${currentTab}"] input[type=search]`); if (f) f.focus(); },
         enabled: () => !!$(`.tabpanel[data-tab="${currentTab}"] input[type=search]`)},
  applyAll: {run: () => applyChanges(), enabled: () => !!slug && siteData && siteData.layout && !applyRunning()},
  replan: {run: () => siteAction("site/plan"), enabled: () => !!slug && isCity() && !activeJobs("site.plan").length},
  build: {run: () => siteAction("site/build"), enabled: () => !!slug && siteData && siteData.layout && !siteBusy()},
  render: {run: () => siteAction("shots/render", {shots: null}), enabled: () => !!slug && siteData && !!siteData.summary && !siteBusy()},
  extract: {run: () => siteAction("site/extract"), enabled: () => !!slug && siteData && !!siteData.summary && !siteBusy()},
  previews: {run: () => siteAction("site/preview"), enabled: () => !!slug && siteData && !!siteData.summary && !siteBusy()},
  frames: {run: () => framesAction({}), enabled: () => !!framesData && framesData.shots.some(r => !r.batches.length) && !activeJobs("frames.generate", {missing: true}).length},
  cutViews: {run: () => { showTab("views"); $("#cut-views").click(); }, enabled: () => reviewData && reviewData.pending_sheets.length && !activeJobs("views.cut").length},
  make3d: {run: () => { showTab("views"); $("#review-list").scrollIntoView(); }, enabled: () => !!reviewData && reviewData.assets.length},
  exportUe: {run: () => ueExport(), enabled: () => !!unrealData && unrealData.greybox && !activeJobs("ue.export").length},
  backupUe: {run: () => ueBackup(), enabled: () => !!unrealData && unrealData.project && !activeJobs("ue.backup").length},
  stopAll: {run: () => stopAll(), enabled: () => allActive().some(j => STOPPABLE(j.kind))},
  comfyCheck: {run: () => comfyDialog()},
  vlmStatus: {run: () => vlmDialog()},
  planImage: {run: () => window.open(fileUrl(catalogData.plan_image, catalogData.plan_mtime), "_blank"), enabled: () => !!(catalogData && catalogData.plan_image)},
  shortcuts: {run: () => shortcutsDialog()},
  about: {run: () => aboutDialog()},
};
const siteBusy = () => SITE_JOBS.some(k => activeJobs(k).length);

async function stopAll(ask = false) {
  const jobs = allActive().filter(j => STOPPABLE(j.kind));
  if (!jobs.length) return;
  if (ask && !confirm(`Stop ${jobs.length} job${jobs.length === 1 ? "" : "s"}?`)) return;
  await cancelJobs(jobs);
}

const MENUS = [
  ["File", "F", [["New project…", "newProject", "Ctrl+N"], ["Open project…", "openProject", "Ctrl+O"], "-",
    ["Refresh", "refresh"]]],
  ["Edit", "E", [["Save", "save", "Ctrl+S"], ["Revert", "revert"], "-", ["Find…", "find", "Ctrl+F"]]],
  ["View", "V", () => TABS.map(t => [TAB_NAMES[t], () => showTab(t), null, () => currentTab === t])],
  ["Pipeline", "P", [["Apply all changes", "applyAll", "Ctrl+Enter"], ["Re-plan only", "replan", "F5"],
    ["Rebuild greybox", "build"], ["Render shot passes", "render"], ["Re-read .blend", "extract"], ["Aerial previews", "previews"], "-",
    ["Generate missing frames", "frames"], ["Cut views", "cutViews"], ["Make 3D…", "make3d"], "-",
    ["Export to Unreal", "exportUe", "Ctrl+E"], ["Back up Unreal project", "backupUe"], "-", ["Stop all jobs", "stopAll", "Esc"]]],
  ["Tools", "T", [["ComfyUI status…", "comfyCheck"], ["Vision LLM status…", "vlmStatus"], "-", ["Open city plan image", "planImage"]]],
  ["Help", "H", [["Keyboard shortcuts", "shortcuts"], "-", ["About Terraformer Pipeline", "about"]]],
];

function buildMenus() {
  const bar = $("#menubar"); bar.replaceChildren();
  MENUS.forEach(([name, key, items], mi) => {
    const m = document.createElement("div"); m.className = "menu";
    const i = name.indexOf(key);
    m.innerHTML = `<button type="button" role="menuitem" aria-haspopup="true" aria-expanded="false">${esc(name.slice(0, i))}<span class="u">${key}</span>${esc(name.slice(i + 1))}</button>
      <div class="dropdown" role="menu" aria-label="${esc(name)}"></div>`;
    m.dataset.key = key.toLowerCase(); m.dataset.index = mi;
    m._items = items;
    bar.append(m);
  });
}
function fillMenu(m) {
  const dd = $(".dropdown", m); dd.replaceChildren();
  const items = typeof m._items === "function" ? m._items() : m._items;
  for (const it of items) {
    if (it === "-") { dd.append(document.createElement("hr")); continue; }
    const [label, act, keys, checked] = it;
    const a = typeof act === "string" ? ACTIONS[act] : {run: act};
    const b = document.createElement("button"); b.type = "button"; b.setAttribute("role", checked ? "menuitemradio" : "menuitem");
    if (checked) { b.setAttribute("role", "menuitem"); b.setAttribute("aria-checked", !!checked()); }
    b.innerHTML = `<span>${esc(label)}</span>${keys ? `<kbd>${esc(keys)}</kbd>` : ""}`;
    b.disabled = a.enabled ? !a.enabled() : false;
    if (a.title) b.title = a.title;
    b.onclick = () => { closeMenus(); a.run(); };
    dd.append(b);
  }
}
let openMenu = null;
function openMenuEl(m, focusFirst = false) {
  closeMenus(false);
  fillMenu(m);
  m.classList.add("open"); $("button", m).setAttribute("aria-expanded", "true");
  $("#menubar").classList.add("active");
  openMenu = m;
  if (focusFirst) { const f = [...m.querySelectorAll(".dropdown button")].find(b => !b.disabled); if (f) f.focus(); }
}
function closeMenus(all = true) {
  document.querySelectorAll("#menubar .menu.open").forEach(m => { m.classList.remove("open"); $("button", m).setAttribute("aria-expanded", "false"); });
  if (all) { $("#menubar").classList.remove("active"); openMenu = null; }
}
buildMenus();
$("#menubar").addEventListener("click", e => {
  const top = e.target.closest(".menu > button"); if (!top) return;
  const m = top.parentElement;
  if (m.classList.contains("open")) closeMenus(); else openMenuEl(m);
});
$("#menubar").addEventListener("mouseover", e => {
  const m = e.target.closest(".menu");
  if (openMenu && m && m !== openMenu) openMenuEl(m);
});
document.addEventListener("mousedown", e => { if (openMenu && !e.target.closest("#menubar")) closeMenus(); });
$("#menubar").addEventListener("keydown", e => {
  if (!openMenu) return;
  const menus = [...document.querySelectorAll("#menubar .menu")], mi = menus.indexOf(openMenu);
  const items = [...openMenu.querySelectorAll(".dropdown button")].filter(b => !b.disabled);
  const ii = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = items.length; if (!n) return;
    items[ii < 0 ? 0 : (ii + (e.key === "ArrowDown" ? 1 : -1) + n) % n].focus();
  } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault();
    openMenuEl(menus[(mi + (e.key === "ArrowRight" ? 1 : -1) + menus.length) % menus.length], true);
  } else if (e.key === "Escape") {
    e.preventDefault(); e.stopPropagation();
    const top = $("button", openMenu); closeMenus(); top.focus();
  }
});

// --- keyboard shortcuts --------------------------------------------------------------------
const typing = el => el && (el.matches("input:not([type=checkbox]):not([type=radio]):not([type=range]), textarea, select") || el.isContentEditable);
const keyHandlers = [];    // pages add (e) => true when they handled a key (Frames: S, arrows)
document.addEventListener("keydown", e => {
  if ($("#dialogs").children.length) return;          // a dialog has the keyboard
  if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.length === 1) {   // Alt+F opens File, etc.
    const m = document.querySelector(`#menubar .menu[data-key="${e.key.toLowerCase()}"]`);
    if (m) { e.preventDefault(); openMenuEl(m, true); return; }
  }
  if (openMenu) return;
  const ctrl = e.ctrlKey || e.metaKey;
  const run = name => { const a = ACTIONS[name]; e.preventDefault(); if (!a.enabled || a.enabled()) a.run(); };
  if (ctrl && e.key === "Enter") return run("applyAll");
  if (ctrl && e.key.toLowerCase() === "s") return run("save");
  if (ctrl && e.key.toLowerCase() === "n" && !e.shiftKey) return run("newProject");
  if (ctrl && e.key.toLowerCase() === "o") return run("openProject");
  if (ctrl && e.key.toLowerCase() === "e") return run("exportUe");
  if (ctrl && e.key.toLowerCase() === "f" && ACTIONS.find.enabled()) return run("find");
  if (e.key === "F5" && !ctrl && isCity()) return run("replan");
  if (e.key === "Escape" && !typing(document.activeElement) && allActive().length) { e.preventDefault(); stopAll(true); return; }
  if (!ctrl && !e.altKey && !typing(document.activeElement)) for (const h of keyHandlers) if (h(e)) { e.preventDefault(); return; }
});

// --- toolbar -------------------------------------------------------------------------------
const TOOLBAR = {"#tb-new": "newProject", "#tb-apply": "applyAll", "#tb-replan": "replan", "#tb-build": "build",
  "#tb-render": "render", "#tb-frames": "frames", "#tb-stop": "stopAll"};
for (const [sel, act] of Object.entries(TOOLBAR)) $(sel).onclick = () => ACTIONS[act].run();

// --- info dialogs ----------------------------------------------------------------------------
async function comfyDialog() {
  const d = dialog({title: "ComfyUI status", body: `<p>Checking…</p>`});
  const s = await api("/api/comfy").catch(e => ({up: false, error: e.message}));
  $(".wbody", d.win).innerHTML = s.up
    ? `<div class="form"><span class="lbl">URL:</span><code>${esc(s.url)}</code><span class="lbl">State:</span><span class="status-ok">connected</span>
       ${s.device ? `<span class="lbl">Device:</span><span>${esc(s.device)}</span>` : ""}</div>`
    : `<p class="error">${esc(s.error || "not reachable")}</p><p class="hint">Start it with <code>~/Projects/AI/ComfyUI/run_comfy.sh</code>.</p>`;
}
async function vlmDialog() {
  const d = dialog({title: "Vision LLM status", body: `<p>Checking…</p>`});
  const s = await api("/api/vlm").catch(e => ({up: false, error: e.message}));
  $(".wbody", d.win).innerHTML = s.up
    ? `<p>Local Qwen3-VL <b>${esc(String(s.model).toUpperCase())}</b> on GPU ${esc(s.gpu)}.</p>`
    : `<p>Not loaded. It starts on demand when a scene is analysed${s.error ? ` (${esc(s.error)})` : ""}.</p>`;
}
function shortcutsDialog() {
  dialog({title: "Keyboard shortcuts", body: `<table class="grid-table"><tbody>
    ${[["Ctrl+Enter", "Apply all changes (re-plan → rebuild greybox → render passes)"], ["F5", "Re-plan the city"],
       ["Ctrl+S", "Save the current form"], ["Ctrl+F", "Find in the current list"], ["Esc", "Stop all jobs (asks first)"],
       ["Ctrl+N / Ctrl+O", "New / open project"], ["Alt+F, E, V, P, T, H", "Open a menu; arrows move, Enter runs"],
       ["S", "Frames: star the frame"], ["← →", "Frames: previous / next frame"], ["↑ ↓", "Frames: previous / next shot"],
       ["1 / 2 / 3", "Frames: shot frame / design reference / reject"]]
      .map(([k, v]) => `<tr><td class="mono">${k}</td><td>${v}</td></tr>`).join("")}</tbody></table>`});
}
function aboutDialog() {
  dialog({title: "About Terraformer Pipeline", body: `<p><b>Terraformer Pipeline</b>: concept images, greybox and shots →
    reviewed frames → asset library → Unreal Engine.</p><p class="hint">Command line: <code>ap</code> (see USAGE.md).
    Fonts: MS Sans Serif from 98.css (MIT), Cousine (SIL OFL 1.1).</p>`});
}

// --- side panel, status line, status bar ---------------------------------------------------
const jobLabel = j => (JOB_LABELS[j.kind] || j.kind).replace(/^./, c => c.toUpperCase()) +
  (j.tag.shot ? ` · ${j.tag.shot}` : j.tag.unit ? ` · ${j.tag.unit}` : j.tag.asset ? ` · ${j.tag.asset}` : "");

function renderLanes() {
  const el = $("#lanes"); el.replaceChildren();
  const jobs = data ? data.jobs : [];
  for (const [lane, name] of LANES) {
    const act = jobs.filter(j => j.lane === lane && isActive(j));
    const run = act.find(j => j.status === "running"), queued = act.filter(j => j.status === "queued").length;
    const row = document.createElement("div"); row.className = "lane";
    const p = run && jobProgress(run);
    row.innerHTML = `<span>${name}:</span><span class="what" title="${run ? esc(p.line) : ""}">${run ? esc(jobLabel(run)) + (p.count ? ` ${p.done} / ${p.total}` : "") : queued ? "waiting" : "idle"}${queued ? ` (+${queued} queued)` : ""}</span>`;
    if (run) {
      row.insertAdjacentHTML("beforeend", `${progressBar(p)}<span class="eta">${esc(p.eta || p.line || "")}</span>`);
      const actions = document.createElement("div"); actions.className = "actions";
      const show = document.createElement("button"); show.type = "button"; show.className = "small"; show.textContent = "Details";
      show.onclick = () => openJobDialog(run.id);
      actions.append(show);
      if (STOPPABLE(run.kind)) { const s = stopButton(act); s.classList.add("small"); actions.append(s); }
      row.append(actions);
    }
    el.append(row);
  }
  const msg = $("#jobs-msg"); msg.replaceChildren();
  if (!jobs.some(isActive)) {
    const last = jobs.filter(j => j.status === "error" || j.status === "canceled").sort((a, b) => b.finished - a.finished)[0];
    if (last && Date.now() / 1000 - last.finished < 120)
      msg.innerHTML = last.status === "error" ? `<span class="error">${esc(jobLabel(last))} failed: ${esc(last.error)}</span>`
        : `<span>${esc(jobLabel(last))} canceled.</span>`;
  }
  const active = jobs.some(isActive);
  if (active && !polling) polling = setInterval(load, 2000);
  if (!active && polling) { clearInterval(polling); polling = null; }
}

// Progress of a running job: counted by the server from the files the job has written
// (frames, passes, sheets); jobs it can't count show a marquee bar and the elapsed time.
const fmtDur = s => s >= 5400 ? `${Math.round(s / 3600 * 10) / 10} h` : s >= 90 ? `${Math.round(s / 60)} min` : `${Math.max(1, Math.round(s))} s`;
function jobProgress(j) {
  const p = (statusData && statusData.progress && statusData.progress[j.id]) || {};
  const det = p.total != null && p.done != null;
  return {done: p.done ?? null, total: p.total ?? null, line: p.line || "",
    count: det ? `${p.done} of ${p.total}` : "",
    eta: p.eta_s != null ? `about ${fmtDur(p.eta_s)} left` : p.elapsed_s != null ? `${fmtDur(p.elapsed_s)} elapsed` : ""};
}
function progressBar(p) {
  const det = p.total && p.done != null;
  const pct = det ? Math.min(100, Math.round(100 * p.done / p.total)) : 0;
  return `<div class="progress${det ? "" : " marquee"}" role="progressbar" aria-valuemin="0" aria-valuemax="100"
    ${det ? `aria-valuenow="${pct}"` : ""}><div class="fill" style="${det ? `width:${pct}%` : ""}"></div></div>`;
}

// --- what's out of date, and Apply changes ----------------------------------------------
// catalog (saved edits) -> plan (city_plan.png) and greybox (greybox.json) -> shot passes.
// Catalog saves are noted per project in this browser (edit.js), since the layout file also
// changes for shot prompt and camera edits, which need none of this.
const catSavedKey = () => `ap.catSaved.${slug}`;
function catalogSavedAt() { try { return +localStorage.getItem(catSavedKey()) || 0; } catch (_) { return 0; } }
function noteCatalogSaved() { try { localStorage.setItem(catSavedKey(), String(Date.now() / 1000)); } catch (_) { /* ignore */ } }

function outOfDate() {
  if (!siteData || !siteData.layout || !statusData) return {plan: false, greybox: false, passes: 0, any: false};
  const st = statusData, saved = catalogSavedAt();
  const plan = isCity() && saved > (st.plan_mtime || 0) + 1;
  // the plan is a report on the layout: re-planning alone never makes the greybox stale
  const greybox = !!siteData.summary && saved > (st.greybox_mtime || 0) + 1;
  const passes = (siteData.shots || []).filter(r => !r.rendered || r.stale).length;
  return {plan, greybox, passes, any: plan || greybox || passes > 0};
}
let applyState = null;     // {steps: [[label, path, body]], i, job, canceled}
const applyRunning = () => !!applyState;

async function applyChanges() {
  if (applyState) return;
  const o = outOfDate();
  const steps = [];
  if (o.plan) steps.push(["Re-planning the city", "site/plan", {}]);
  if (o.greybox || o.plan) steps.push(["Rebuilding the greybox", "site/build", {}]);
  if (steps.length) steps.push(["Rendering shot passes", "shots/render", {shots: null}]);
  else if (o.passes) steps.push(["Rendering shot passes", "shots/render",
    {shots: siteData.shots.filter(r => !r.rendered || r.stale).map(r => r.id)}]);
  if (!steps.length) { dialog({title: "Apply changes", body: "<p>Nothing is out of date.</p>"}); return; }
  applyState = {steps, i: 0, job: null, canceled: false};
  renderShell();
  try {
    for (; applyState.i < steps.length; applyState.i++) {
      const [, path, body] = steps[applyState.i];
      let job;
      try { job = await api(`/api/projects/${slug}/${path}`, body); }
      catch (err) {
        if (!err.message.includes("edited in Blender")) throw err;
        if (!confirm(`${err.message}. Rebuild anyway? (the old file is kept as greybox.prev.blend)`)) { applyState.canceled = true; break; }
        job = await api(`/api/projects/${slug}/${path}`, {...body, force: true});
      }
      applyState.job = job.id;
      await load();
      openJobDialog(job.id);
      while (true) {                 // wait for this step's job
        await new Promise(r => setTimeout(r, 1500));
        job = await api(`/api/jobs/${job.id}`);
        if (!isActive(job)) break;
      }
      if (job.status !== "done") { applyState.canceled = job.status === "canceled"; if (job.status === "error") throw new Error(job.error); break; }
    }
  } catch (err) { alert(`Apply changes stopped: ${err.message}`); }
  finally { applyState = null; await load(); }
}

function renderApplyBar() {
  document.querySelectorAll(".applybar-slot").forEach(s => s.replaceChildren());
  if (!["site", "city", "shots", "frames"].includes(currentTab)) return;
  const slot = $(`.tabpanel[data-tab="${currentTab}"] .applybar-slot`);
  const o = outOfDate();
  if (!slot || (!o.any && !applyState)) return;
  const bar = document.createElement("div"); bar.className = "applybar"; bar.setAttribute("role", "status");
  if (applyState) {
    const [label] = applyState.steps[applyState.i] || ["Finishing"];
    bar.innerHTML = `<span class="msg">Applying changes: step ${applyState.i + 1} of ${applyState.steps.length} · ${esc(label)}…</span>`;
    const show = document.createElement("button"); show.type = "button"; show.textContent = "Details";
    show.onclick = () => applyState && applyState.job && openJobDialog(applyState.job);
    bar.append(show);
  } else {
    const what = [o.plan && "the plan", o.greybox && "the greybox", o.passes && `${o.passes} shot pass${o.passes === 1 ? "" : "es"}`].filter(Boolean);
    const list = what.length > 1 ? `${what.slice(0, -1).join(", ")} and ${what.at(-1)}` : what[0];
    bar.innerHTML = `<span class="msg">${o.plan ? `Catalog changed: ${esc(list)}` : esc(list.replace(/^./, c => c.toUpperCase()))} ${what.length > 1 || o.passes > 1 ? "are" : "is"} out of date.</span>`;
    const b = document.createElement("button"); b.type = "button"; b.className = "default"; b.textContent = "Apply changes";
    b.title = "Re-plan → rebuild greybox → render passes, each only if needed (Ctrl+Enter)";
    b.disabled = !ACTIONS.applyAll.enabled() || siteBusy();
    b.onclick = () => applyChanges();
    bar.append(b);
  }
  slot.append(bar);
}

// Stage list: what each stage needs. state: "Done", "Out of date", "Running" or "".
function stageStates() {
  const jobs = kinds => kinds.some(k => activeJobs(k).length);
  const sh = siteData ? siteData.shots : [];
  const passesStale = sh.filter(r => !r.rendered || r.stale).length;
  const fr = framesData ? framesData.shots : [];
  const ood = outOfDate();
  const anyStar = pre => Object.keys(data.stars).some(k => data.stars[k] && k.startsWith(pre));
  return [
    ["style", "Style", jobs(["style.explore", "style.derive", "style.draft"]) ? "Running" : data.project.anchor ? "Done" : ""],
    ["city", "City", jobs(["site.plan", "site.build", "site.extract"]) ? "Running" : !siteData.layout ? "" :
      !siteData.summary || siteData.summary.stale || ood.plan || ood.greybox ? "Out of date" : "Done"],
    ["shots", "Shots", jobs(["shots.render", "shots.camera"]) ? "Running" : !sh.length ? "" : passesStale ? "Out of date" : "Done"],
    ["frames", "Frames", jobs(["frames.generate"]) ? "Running" : !fr.length ? "" :
      fr.some(r => !r.batches.length) ? "Out of date" : anyStar("frames/") ? "Done" : ""],
    ["library", "Library", jobs(["plan.analyze", "plan.refine", "refs.generate"]) ? "Running" : plans.length ? "Done" : ""],
    ["views", "Views and 3D", jobs(["views.cut", "3d.trellis", "cleanup"]) ? "Running" :
      reviewData && reviewData.assets.some(a => a.results.length) ? "Done" : ""],
    ["unreal", "Unreal", jobs(["ue.export", "ue.backup"]) ? "Running" : !unrealData || !unrealData.export ? "" :
      ueExportStale() ? "Out of date" : "Done"],
  ];
}

function renderStages() {
  const ul = $("#stage-list"); ul.replaceChildren();
  if (!data) return;
  stageStates().forEach(([tab, name, state], i) => {
    const li = document.createElement("li"); li.setAttribute("role", "option");
    li.setAttribute("aria-selected", tab === currentTab || (currentTab === "site" && tab === "city"));
    const cls = state === "Done" ? "state-ready" : state === "Out of date" ? "state-stale" : "";
    li.innerHTML = `<span><span class="n">${i + 1}</span>${esc(name)}</span><span class="${cls}">${esc(state)}</span>`;
    li.onclick = () => showTab(tab);
    ul.append(li);
  });
}

function renderShell() {
  const proj = slug ? ` · ${slug}` : "";
  document.title = `Terraformer Pipeline${proj}`;
  $("#titlebar").textContent = `Terraformer Pipeline${proj}`;
  for (const t of TABS) document.querySelector(`#tabs [data-tab="${t}"]`).disabled = !slug;
  if (!data || !slug) { $("#status-line").textContent = "No project"; renderLanes(); return; }
  $("#empty").hidden = true;
  const active = allActive();
  $("#status-line").textContent = `${active.length ? "Working" : "Ready"} · ${active.length ? `${active.length} job${active.length === 1 ? "" : "s"} running` : "no jobs"}`;
  $("#city-none").hidden = isCity();
  renderStages();
  renderLanes();
  for (const [sel, act] of Object.entries(TOOLBAR)) { const a = ACTIONS[act]; $(sel).disabled = a.enabled ? !a.enabled() : false; }
  $("#tb-replan").hidden = !isCity();
  const comfyRun = active.find(j => j.lane === "comfy" && j.status === "running");
  const cp = comfyRun && jobProgress(comfyRun);
  $("#sb-gpu").textContent = comfyRun ? `GPU 1: ${JOB_LABELS[comfyRun.kind] || comfyRun.kind}${cp.count ? ` ${cp.done} / ${cp.total}` : ""}` : "GPU 1: idle";
  $("#sb-left").textContent = (statusLeft[currentTab] && statusLeft[currentTab]()) || TAB_NAMES[currentTab];
  const r = catalogData && catalogData.report;
  $("#sb-mid").textContent = (statusMid[currentTab] && statusMid[currentTab]()) ||
    (r && r.buildings ? `${r.buildings.toLocaleString("en")} buildings · ${r.warnings.length} warning${r.warnings.length === 1 ? "" : "s"}` : "");
  const comfy = statusData && statusData.comfy;
  const right = statusRight[currentTab] && statusRight[currentTab]();
  $("#sb-right").textContent = right || (comfy == null ? "" : comfy ? "ComfyUI: connected" : "ComfyUI: not running");
  $("#sb-right").className = !right && comfy === false ? "error" : "";
  if (comfy != null) $("#status-line").textContent += comfy ? " · ComfyUI connected" : " · ComfyUI not running";
  renderApplyBar();
  updateJobDialog();
}
