// The tabs as one linear workflow: what each step is, an honest status computed from the project's
// real state (files, jobs, decisions; never from one file existing), and what the next step needs.
// Used by the header strip on every tab, the Stages list (shell.js) and the guide panel.
// Statuses, in the words the UI shows:
const ST = {none: "Not started", input: "Needs input", ready: "Ready", running: "Running", review: "Needs review",
  stale: "Out of date", done: "Complete"};

// Steps in tab order. Site and City are one step: City is the procedural layout, Site your own
// Blender layout; a project uses one or the other.
const STEPS = [
  {id: "style", tabs: ["style"], name: "Style", desc: "explore looks and save a style anchor (optional: frames use its style text)."},
  {id: "layout", tabs: ["city", "site"], name: "Site / City", desc: {
    city: "lay out the city procedurally from building types and districts, then build the greybox.",
    site: "build the greybox from your own layout, or edit it in Blender and re-read it."},
    note: "City = procedural layout, Site = your own Blender layout; use one or the other."},
  {id: "shots", tabs: ["shots"], name: "Shots", desc: "render each shot camera's depth, edge and object-id passes from the greybox."},
  {id: "frames", tabs: ["frames"], name: "Frames", desc: "generate concept frames per shot and lock the one to use for each shot."},
  {id: "library", tabs: ["library"], name: "Library", desc: "discover the assets the locked shots show, decide how each is made, and build them."},
  {id: "views", tabs: ["views"], name: "Views and 3D", desc: "extract reference views from starred sheets and make 3D models (TRELLIS) for props and irregular assets."},
  {id: "unreal", tabs: ["unreal"], name: "Unreal", desc: "export, import into Unreal, render the shots and match the look to the approved frames."},
];
const stepOfTab = tab => STEPS.find(s => s.tabs.includes(tab));
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const anyJob = kinds => kinds.some(k => activeJobs(k).length);

// Each returns {status (an ST key), detail, next: {ok, reason}}; next is what the following step needs.
const STEP_STATE = {
  style() {
    const a = data.project.anchor, scenes = data.explore.length, board = Object.keys(data.moodboard || {}).length;
    const st = anyJob(["style.explore", "style.derive", "style.draft"]) ? "running"
      : a && ((a.images || []).length || a.style_text) ? "done" : scenes || board ? "review" : "none";
    const detail = st === "done" ? ((a.images || []).length ? `anchor saved: ${plural(a.images.length, "image")}${a.style_text ? " and style text" : ""}` : "style text saved (no anchor images)")
      : st === "review" ? "star scenes or objects, then save the style anchor (or just the style text)"
      : st === "none" ? "optional: skip it to work without a style anchor" : "";
    return {status: st, detail, next: {ok: true}};
  },
  layout() {
    const s = siteData, o = outOfDate(), sum = s.summary;
    const run = anyJob(["site.plan", "site.build", "site.extract", "site.preview"]) || applyRunning();
    const stale = sum && (sum.stale || o.plan || o.greybox);
    const st = run ? "running" : !s.layout ? "input" : !sum ? "ready" : stale ? "stale" : "done";
    const detail = !s.layout ? "start from the starter city (City tab) or the starter layout (Site tab)"
      : !sum ? "layout ready: build the greybox"
      : stale ? [o.plan && "the catalog changed after the last plan", o.greybox && "the catalog changed after the last greybox",
        sum.stale && "the .blend changed since it was last read (Re-read .blend)"].filter(Boolean).join("; ")
      : `greybox built: ${plural(sum.slots, "slot")}${isCity() ? " from the city plan" : ""}`;
    return {status: st, detail, next: {ok: !!sum, reason: "Build the greybox first."}};
  },
  shots() {
    const sh = siteData.shots || [], bad = sh.filter(r => !r.rendered || r.stale), fresh = sh.length - bad.length;
    const st = !sh.length ? "none" : anyJob(["shots.render", "shots.camera"]) ? "running"
      : !sh.some(r => r.rendered) ? "ready" : bad.length ? "stale" : "done";
    const detail = !sh.length ? "no shots yet: build the greybox (Site / City)"
      : st === "ready" ? `${plural(sh.length, "shot")} to render` : bad.length ? `${bad.length} of ${plural(sh.length, "shot")}: passes missing or stale`
      : `${plural(sh.length, "shot")}, every pass current`;
    return {status: st, detail, next: {ok: fresh > 0, reason: "Render the shot passes first."}};
  },
  frames() {
    const rows = framesData ? framesData.shots : [], real = rows.filter(r => !r.mood);
    const locked = real.filter(r => r.locked), withFrames = rows.filter(r => r.batches.length);
    const unreviewed = rows.flatMap(r => r.batches.filter(b => !b.archived).flatMap(b => b.frames)).filter(f => !f.role).length;
    const st = !rows.length ? "none" : anyJob(["frames.generate"]) ? "running"
      : real.length && locked.length === real.length ? "done" : !withFrames.length && !locked.length ? "ready" : "review";
    const detail = !rows.length ? "no shots yet" : st === "ready" ? "generate frames for the shots"
      : `${locked.length} of ${plural(real.length, "shot")} locked${unreviewed ? ` · ${plural(unreviewed, "frame")} not reviewed` : ""}`;
    return {status: st, detail, next: {ok: locked.length > 0, reason: "Lock at least one shot (Use this frame for this shot)."}};
  },
  library() {
    const plan = libraryData && libraryData.plan, run = anyJob(["library.plan", "library.check", "refs.generate",
      "library.brief_build", "library.rebuild", "library.guide"]);
    const lockedFrames = (framesData ? framesData.shots : []).filter(r => r.locked && r.locked !== "greybox" && !r.mood);
    const starred = reviewData ? reviewData.starred_sheets : 0;
    const next = {ok: starred > 0 || !!(reviewData && reviewData.assets.some(a => a.results.length)),
      reason: "Star at least one reference sheet (Library › Reference sheets)."};
    if (!plan) return {status: run ? "running" : lockedFrames.length ? "ready" : "none", next,
      detail: lockedFrames.length ? "discover the assets the locked shots show" : (libraryPrereq() || {text: ""}).text.replace(/^No assets yet: /, "")};
    // a frame locked after the plan, or a different frame now locked: the plan never considered it
    // (frames it considered and dropped as an older layout are in candidates too, and stay dropped)
    const considered = new Set((plan.candidates || []).map(c => c.frame));
    const missing = lockedFrames.filter(r => !considered.has(r.locked));
    const ents = libEntries(), dec = e => libDecision(e);
    const undecided = ents.filter(e => !dec(e)).length, included = ents.filter(e => dec(e) && dec(e) !== "exclude");
    const sheeted = new Set(refs.filter(u => u.plan === "library" && u.sheets.length).map(u => u.key));
    const builds = (lr.list && lr.list.assets) || [], approved = builds.filter(b => b.status === "approved").length;
    const noSheet = included.filter(e => !sheeted.has(e.id) && !lrAssetsOf(e.id).length).length;
    const st = run ? "running" : missing.length ? "stale" : undecided ? "input" : noSheet ? "ready"
      : builds.length && approved < builds.length ? "review" : included.length ? "done" : "input";
    const detail = missing.length ? `${plural(missing.length, "locked shot")} not in the asset plan (${missing.map(r => r.id).join(", ")}): Discover assets again`
      : [undecided && `${undecided} of ${plural(ents.length, "asset")} not decided`, noSheet && `${noSheet} without reference sheets`,
        builds.length && `${approved} of ${plural(builds.length, "build")} approved`].filter(Boolean).join(" · ") || `${plural(included.length, "asset")} decided`;
    return {status: st, detail, next};
  },
  views() {
    const r = reviewData;
    if (!r) return {status: "none", detail: "", next: {ok: false, reason: "Loading…"}};
    const withViews = r.assets.filter(a => a.views.length), with3d = r.assets.filter(a => a.results.length);
    const waiting = withViews.filter(a => !a.results.length).length;
    const unapproved = with3d.filter(a => a.review != null ? a.review !== "approved" : false).length;
    const st = anyJob(["views.cut", "3d.trellis", "cleanup"]) ? "running" : !r.starred_sheets && !with3d.length ? "none"
      : r.pending_sheets.length ? "ready" : waiting ? "input" : unapproved ? "review" : with3d.length ? "done" : "ready";
    const detail = st === "none" ? "star reference sheets in Library › Reference sheets first"
      : [r.pending_sheets.length && `${plural(r.pending_sheets.length, "starred sheet")} to extract views from`,
        waiting && `${plural(waiting, "asset")} with views but no 3D model`, unapproved && `${unapproved} not approved`]
        .filter(Boolean).join(" · ") || `${plural(with3d.length, "asset")} with a 3D model`;
    return {status: st, detail, next: {ok: !!siteData.summary, reason: "Build the greybox first (Site / City)."}};
  },
  unreal() {
    const u = unrealData;
    if (!u) return {status: "none", detail: "", next: {ok: false}};
    const run = (data.jobs || []).some(j => isActive(j) && j.kind.startsWith("ue."));
    const st = run ? "running" : !u.greybox ? "none" : !u.export ? "ready" : ueExportStale() ? "stale" : !u.project ? "input" : "done";
    const detail = !u.greybox ? "build the greybox first (Site / City)" : !u.export ? "export the greybox and library for Unreal"
      : ueExportStale() ? "the greybox changed after the export: export again" : !u.project ? "create the UE project"
      : "exported, UE project ready: import, render and match the look";
    return {status: st, detail, next: {ok: false, reason: "This is the last step."}};
  },
};

// [{...step, status, detail, next}] for the whole workflow, from the data already loaded
function workflow() {
  if (!data || !siteData) return [];
  return STEPS.map(s => {
    let r;
    try { r = STEP_STATE[s.id](); } catch (err) { r = {status: "none", detail: "", next: {ok: false, reason: ""}}; console.warn(s.id, err); }
    return {...s, ...r};
  });
}

// --- empty states: the real missing prerequisite for the library (Reference sheets, Views and 3D) ---
// {text, go: [tab, sub], label}: world/city projects go greybox → locked frame → Discover assets →
// decisions → sheets; a project with no site layout (scene mode) still starts from Scene plans.
function libraryPrereq(views = false) {
  const go = (text, tab, sub, label) => ({text, go: [tab, sub], label});
  if (!siteData || !siteData.layout) return go("No assets yet: analyse a scene in Library › Scene plans.", "library", "plan", "Open Scene plans");
  const lay = isCity() ? "city" : "site";
  if (!siteData.summary) return go("No assets yet: build the greybox first.", lay, null, `Open ${TAB_NAMES[lay]}`);
  const locked = (framesData ? framesData.shots : []).filter(r => r.locked && r.locked !== "greybox" && !r.mood);
  if (!locked.length) return go("No assets yet: lock a shot on its approved frame (Frames › Use this frame for this shot).", "frames", null, "Open Frames");
  if (!libraryData || !libraryData.plan) return go("No assets yet: discover them from the locked shots (Library › Discover assets from locked shots).", "library", "assets", "Open Asset review");
  const ents = libEntries(), included = ents.filter(e => libDecision(e) && libDecision(e) !== "exclude");
  if (!included.length) return go("No assets included yet: decide each asset in Library › Asset review.", "library", "assets", "Open Asset review");
  if (views) return go("No views yet: star reference sheets in Library › Reference sheets, then Cut views.", "library", "refs", "Open Reference sheets");
  return null;
}
// the hint as HTML with a button that goes there (wired by wireGo)
// (data-go-*, never data-tab / data-sub: showTab and showSub hide every element carrying those)
const prereqHtml = p => `${esc(p.text)} <button type="button" class="small prereq-go" data-go-tab="${p.go[0]}" data-go-sub="${p.go[1] || ""}">${esc(p.label)}</button>`;
function wireGo(el) {
  el.querySelectorAll(".prereq-go").forEach(b => b.onclick = () => { showTab(b.dataset.goTab); if (b.dataset.goSub) showSub(b.dataset.goSub); });
}

// --- header strip ------------------------------------------------------------------------------
function stepHead(tab) {
  const panel = $(`section.tabpanel[data-tab="${tab}"]`);
  if (!panel) return null;
  let el = $(":scope > .step-head", panel);
  if (!el) { el = document.createElement("div"); el.className = "step-head"; el.setAttribute("role", "status"); panel.prepend(el); }
  return el;
}

function renderStepHeads() {
  if (!data || !slug) return;
  const wf = workflow(), tab = currentTab, el = stepHead(tab);
  if (!el || !wf.length) return;
  const i = wf.findIndex(s => s.tabs.includes(tab)), s = wf[i], nxt = wf[i + 1];
  const behind = wf.slice(0, i).filter(x => x.status === "stale");
  const desc = typeof s.desc === "string" ? s.desc : s.desc[tab];
  const nextTab = nxt && nxt.tabs[0] === "city" && !isCity() ? "site" : nxt && nxt.tabs[0];
  const sig = JSON.stringify([tab, s.status, s.detail, s.next, behind.map(x => x.id), nextTab]);
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.innerHTML = `<div class="sh-main"><span class="sh-step">This step:</span> ${esc(desc)}
      ${s.note ? `<span class="hint sh-note" style="margin:0">${esc(s.note)}</span>` : ""}</div>
    <span class="st-chip st-${s.status}" title="${esc(s.detail)}">${esc(ST[s.status])}</span>
    <span class="sh-detail">${esc(s.detail)}</span>
    ${behind.map(b => `<span class="sh-behind">${esc(b.name)} is out of date: <button type="button" class="link sh-go" data-go-tab="${b.tabs[0] === "city" && !isCity() ? "site" : b.tabs[0]}">revisit ${esc(TAB_NAMES[b.tabs[0] === "city" && !isCity() ? "site" : b.tabs[0]])}</button></span>`).join("")}
    <span class="spacer"></span>
    ${nxt ? `<span class="sh-reason">${s.next.ok ? "" : esc(s.next.reason || "")}</span>
      <button type="button" class="default sh-next" data-go-tab="${nextTab}" ${s.next.ok ? "" : "disabled"}
        title="${esc(s.next.ok ? `Go to ${TAB_NAMES[nextTab]}` : s.next.reason || "")}">Next: ${esc(TAB_NAMES[nextTab])} →</button>`
      : `<span class="sh-reason">${esc(s.next.reason || "")}</span>`}`;
  el.querySelectorAll("[data-go-tab]").forEach(b => b.onclick = () => showTab(b.dataset.goTab));
}
