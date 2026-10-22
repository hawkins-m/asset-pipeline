// Long-job warning (server: estimates.py, app.py ESTIMATE_ROUTES).
// - Before api() POSTs to a route that starts a job, it asks the same route how long the job would
//   take (X-AP-Estimate: 1: the handler runs its checks and counts, then stops short of starting).
//   Over LONG_S (10 min) a dialog says what will run, how many items, about how long, which lanes
//   it holds and what that blocks: Start or Cancel. A canceled start throws START_CANCELED, which
//   the callers' alert() ignores. Batches that already confirm (Library › Areas) pass
//   {confirmed: true} and show the same lines in their own dialog.
// - Job buttons (EST_BUTTONS) get "Estimate: about N min on GPU 0" in their tooltip on hover.
const START_CANCELED = "Not started.";
{
  const nativeAlert = window.alert.bind(window);
  window.alert = msg => { if (msg !== START_CANCELED) nativeAlert(msg); };
}
const EST = {routes: null, long: 600, kinds: null, kindsAt: 0};

async function estRoutes() {
  if (EST.routes) return EST.routes;
  try {
    const r = await (await fetch("/api/estimate/routes")).json();
    EST.long = r.long_s || EST.long;
    EST.routes = r.routes.map(t => new RegExp("^" + t.replace(/\{[^}]+\}/g, "[^/]+") + "$"));
  } catch (_) { EST.routes = []; }
  return EST.routes;
}

// The estimate of the job a POST would start, or null (not a job route, or it wouldn't start one).
async function jobEstimate(path, body) {
  const route = path.split("?")[0];
  if (!(await estRoutes()).some(re => re.test(route))) return null;
  try {
    const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-AP-Estimate": "1"},
                                 body: JSON.stringify(body ?? {})});
    if (!r.ok) return null;
    const j = await r.json();
    return (j && j.estimate) || null;
  } catch (_) { return null; }
}

const estKindLabel = k => (typeof JOB_LABELS !== "undefined" && JOB_LABELS[k]) || k;
// The dialog's lines (also used inside the Areas batch confirm)
function longJobHtml(e) {
  const waitLine = e.wait_s >= 60 ? `<p>Starts after about ${fmtDur(e.wait_s)}: ${e.ahead} job${e.ahead === 1 ? "" : "s"} ahead on ${esc(e.lane_names[0])}.</p>` : "";
  return `<p class="long-job"><b>About ${fmtDur(e.seconds)}</b> for ${e.items} item${e.items === 1 ? "" : "s"} (${esc(estKindLabel(e.kind))}),
      on ${esc(e.lane_names.join(", then "))}.</p>
    <ul class="long-job-blocks">${e.blocks.map(b => `<li>${esc(b)}.</li>`).join("")}</ul>${waitLine}
    <p class="hint">Estimate from ${esc(e.source)}. Stop (Jobs panel) or Stop jobs cancels it cleanly.</p>`;
}

// api()'s gate: true to go ahead.
async function longJobGate(path, body) {
  const e = await jobEstimate(path, body);
  if (!e || !e.long) return true;
  return confirmDialog({title: `Long job: about ${fmtDur(e.seconds)}`, button: "Start", id: "long-job-dialog", body: longJobHtml(e)});
}

// --- button tooltips ------------------------------------------------------------------------
async function estKinds() {
  if (EST.kinds && Date.now() - EST.kindsAt < 60000) return EST.kinds;
  try { const r = await (await fetch("/api/estimate/kinds")).json(); EST.kinds = r.kinds; EST.kindsAt = Date.now(); EST.long = r.long_s || EST.long; }
  catch (_) { EST.kinds = EST.kinds || {}; }
  return EST.kinds;
}
const estMissingFrames = () => framesData ? framesData.shots.filter(r => !(r.batches || []).length && !r.locked && !r.mood).length : 0;
// [selector, kind, items(), variant()] for the buttons that start jobs
const EST_BUTTONS = [
  [".make3d", "3d.trellis", () => 1, () => ($("#trellis-mode") || {}).value || "1024_cascade"],
  [".reroll, .reroll-go", "library.reroll"],
  [".asset-review .cleanup", "cleanup"],
  ["#cut-views", "views.cut", () => reviewData ? reviewData.pending_sheets.length : 1],
  ["#site-build", "site.build"], ["#site-extract", "site.extract"], ["#site-preview", "site.preview"],
  ["#shots-render", "shots.render", () => siteData && siteData.shots ? siteData.shots.length : 1],
  ["#frames-missing", "frames.generate", () => estMissingFrames() * framesPerShot()],
  ["#refs-missing", "refs.generate", () => refs.filter(u => !u.sheets.length).length * 2],
  ["#lib-check", "library.check"], [".next-brief", "library.brief_build"],
  ["#ue-export", "ue.export"], ["#ue-import", "ue.import"], ["#ue-look", "ue.look"], ["#ue-backup", "ue.backup"],
  ["#ue-render", "ue.render", () => siteData && siteData.shots ? siteData.shots.length : 1],
];
const EST_MARK = "\nEstimate: ";
async function estDecorate(el) {
  const hit = EST_BUTTONS.find(([sel]) => el.matches(sel));
  if (!hit) return;
  const [, kind, itemsFn, varFn] = hit;
  const ks = await estKinds(), k = ks[varFn ? `${kind}/${varFn()}` : kind] || ks[kind];
  if (!k) return;
  const n = Math.max(1, (itemsFn && itemsFn()) || 1), s = n * k.per_item_s;
  const base = (el.title || "").split(EST_MARK)[0];
  el.title = `${base}${EST_MARK}about ${fmtDur(s)}${n > 1 ? ` for ${n} items` : ""} on ${k.lane_names.join(", then ")}${s > EST.long ? " (asks before it starts)" : ""}.`;
}
document.addEventListener("mouseover", ev => {
  const el = ev.target.closest && ev.target.closest("button");
  if (el && !el.disabled) estDecorate(el);
}, true);
