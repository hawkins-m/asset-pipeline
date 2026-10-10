// Job dialogs ("loaders"): one classic window per running job. The picture shows what kind
// of work it is (CSS animations only; off under prefers-reduced-motion); the bar and the
// lines under it show the job's real progress (counted by the server where it can be, else
// a marquee and the elapsed time). Cancel stops the job like the Stop buttons do; the close
// box only hides the window (the job keeps running; Details in the Jobs panel reopens it).
const INK = `stroke="#333" stroke-width="1.5" fill="none"`;
const PICS = {
  frames: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <rect x="44" y="6" width="52" height="66" fill="#F4F2EA" stroke="#333" stroke-width="1.5"/>
    <path d="M52 18h30M52 26h36M52 34h22" stroke="#666" stroke-width="1.5"/>
    <path d="M54 62l10-12 8 8 6-6 10 10" fill="none" stroke="#316AC5" stroke-width="2"/>
    <g class="ld-lens"><circle cx="82" cy="40" r="10" fill="rgba(255,255,255,.35)" stroke="#333" stroke-width="2.5"/>
      <path d="M89 47l10 10" stroke="#7A4A12" stroke-width="4" stroke-linecap="round"/></g></svg>`,
  greybox: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <path d="M24 70h92" ${INK}/>
    <rect class="ld-rise" style="animation-delay:0s" x="38" y="46" width="18" height="24" fill="#BDBDBA" stroke="#333" stroke-width="1.5"/>
    <g class="ld-rise" style="animation-delay:.35s"><rect x="60" y="30" width="20" height="40" fill="#C9C9C6" stroke="#333" stroke-width="1.5"/>
      <path d="M60 30l10-10 10 10z" fill="#BDBDBA" stroke="#333" stroke-width="1.5"/></g>
    <rect class="ld-rise" style="animation-delay:.7s" x="84" y="52" width="16" height="18" fill="#BDBDBA" stroke="#333" stroke-width="1.5"/></svg>`,
  passes: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <circle class="ld-blink" cx="19" cy="26" r="3.5" fill="#C0392B"/>
    <rect x="14" y="33" width="30" height="22" fill="#555" stroke="#333" stroke-width="1.5"/><path d="M44 39l10-5v20l-10-5z" fill="#555" stroke="#333" stroke-width="1.5"/>
    <svg x="60" y="28" width="68" height="32" viewBox="0 0 68 32"><rect width="68" height="32" fill="#222"/>
      <g class="ld-film"><g id="ld-strip">
        <rect x="3" y="4" width="20" height="24" fill="#EEE"/><rect x="3" y="4" width="20" height="10" fill="#999"/>
        <rect x="26" y="4" width="20" height="24" fill="#222" stroke="#DDD"/><path d="M29 25l7-16 7 16z" fill="none" stroke="#DDD" stroke-width="1.2"/>
        <rect x="49" y="4" width="20" height="24" fill="#5B7BD5"/><rect x="49" y="16" width="10" height="12" fill="#D58B5B"/><rect x="59" y="10" width="10" height="18" fill="#7BC27B"/>
      </g><use href="#ld-strip" x="69"/></g></svg></svg>`,
  scene: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <g class="ld-eye"><path d="M14 40c8-11 30-11 38 0-8 11-30 11-38 0z" fill="#fff" stroke="#333" stroke-width="1.8"/>
      <circle cx="33" cy="40" r="6" fill="#316AC5" stroke="#333"/><circle cx="33" cy="40" r="2.4" fill="#111"/></g>
    <rect x="66" y="8" width="56" height="64" fill="#F4F2EA" stroke="#333" stroke-width="1.5"/>
    ${[22, 36, 50].map((y, i) => `<path d="M84 ${y}h30" stroke="#666" stroke-width="1.5"/>
      <path class="ld-check" style="animation-delay:${i * .6}s" d="M72 ${y}l3 3 6-7" fill="none" stroke="#135A13" stroke-width="2"/>`).join("")}
    <path d="M84 62h30" stroke="#666" stroke-width="1.5"/></svg>`,
  cut: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <rect x="30" y="10" width="80" height="56" fill="#F4F2EA" stroke="#333" stroke-width="1.5"/>
    <rect x="40" y="20" width="10" height="26" fill="#999"/><rect x="64" y="16" width="12" height="30" fill="#999"/><rect x="90" y="24" width="10" height="22" fill="#999"/>
    <path d="M24 54h92" stroke="#333" stroke-dasharray="4 3"/>
    <g class="ld-scissors"><circle cx="34" cy="50" r="3.2" fill="none" stroke="#C0392B" stroke-width="1.8"/><circle cx="34" cy="58" r="3.2" fill="none" stroke="#C0392B" stroke-width="1.8"/>
      <path d="M37 51l12 4M37 57l12-4" stroke="#333" stroke-width="1.8"/></g></svg>`,
  model: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <rect x="14" y="22" width="36" height="36" fill="#E8F0F8" stroke="#333" stroke-width="1.5"/>
    <path d="M16 56l10-14 8 9 6-5 8 10z" fill="#3E8E4E"/><circle cx="40" cy="30" r="3.5" fill="#E9A23B"/>
    ${[60, 68, 76].map((x, i) => `<circle class="ld-dot" style="animation-delay:${i * .25}s" cx="${x}" cy="40" r="2.2" fill="#316AC5"/>`).join("")}
    <g class="ld-cube"><path d="M104 24l16 8v18l-16 8-16-8V32z" fill="#DADAD7" stroke="#333" stroke-width="1.5"/>
      <path d="M88 32l16 8 16-8M104 40v18" fill="none" stroke="#333" stroke-width="1.5"/><path d="M104 40l16-8v18l-16 8z" fill="#BDBDBA"/></g></svg>`,
  mesh: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <path d="M40 66h60l4 6H36z" fill="#BDBDBA" stroke="#333" stroke-width="1.5"/>
    <path class="ld-spiky" d="M44 66l3-30 6 12 5-24 6 16 6-20 5 18 7-14 4 22 6-10 3 30z" fill="#E6E6E3" stroke="#333" stroke-width="1.5"/>
    <path class="ld-smooth" d="M44 66c0-26 10-40 26-40s26 14 26 40z" fill="#E6E6E3" stroke="#333" stroke-width="1.5"/></svg>`,
  plan: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <rect x="30" y="8" width="80" height="64" fill="#E6E2CF" stroke="#333" stroke-width="1.5"/>
    <path d="M30 60c20-6 40 2 80-10v22H30z" fill="#9CB9D6"/>
    <path class="ld-road" d="M70 40L36 14M70 40l34-24M70 40v24M70 40l-30 14M70 40l32 10" fill="none" stroke="#7A4A12" stroke-width="2"/>
    <circle cx="70" cy="40" r="5" fill="#BDBDBA" stroke="#333" stroke-width="1.5"/></svg>`,
  sheet: `<svg viewBox="0 0 140 80" aria-hidden="true">
    <rect x="20" y="14" width="100" height="52" fill="#fff" stroke="#333" stroke-width="1.5"/>
    ${[38, 70, 102].map((x, i) => `<g class="ld-appear" style="animation-delay:${i * .5}s"><path d="M${x - 9} 56h18l-3-24h-12z" fill="#C9A06B" stroke="#333" stroke-width="1.2"/>
      <path d="M${x - 10} 32h20" stroke="#333" stroke-width="1.5"/></g>`).join("")}</svg>`,
};
const LOADERS = {
  "frames.generate": ["Generating concept frames…", "frames", "frames"], "style.explore": ["Generating scenes…", "frames", ""],
  "style.derive": ["Deriving anchor objects…", "cut", ""], "site.build": ["Building greybox…", "greybox", ""],
  "site.extract": ["Reading the .blend…", "greybox", ""], "site.plan": ["Planning the city…", "plan", ""],
  "shots.render": ["Rendering shot passes…", "passes", "shots"], "site.preview": ["Rendering aerial previews…", "passes", ""],
  "shots.camera": ["Moving the camera…", "passes", ""], "plan.analyze": ["Reading the scene…", "scene", ""],
  "plan.refine": ["Refining boxes…", "scene", ""], "style.draft": ["Drafting style text…", "scene", ""],
  "refs.generate": ["Drawing reference sheets…", "sheet", "sheets"], "views.cut": ["Cutting views…", "cut", "sheets"],
  "3d.trellis": ["Building 3D model…", "model", ""], "cleanup": ["Cleaning up mesh…", "mesh", ""],
};
const LANE_NAMES = {gpu0: "GPU 0", comfy: "GPU 1", cpu: "the CPU"};
const base = k => String(k || "").split("/").pop();

function detailLine(j, p) {
  if (p.line) return p.line;
  const t = j.tag;
  return {
    "site.build": "Blender on the CPU · plots, kits, city massing and shot cameras",
    "site.extract": "Blender on the CPU · reading slots, tags and cameras back",
    "site.plan": "City plan without Blender · districts, blocks and building types",
    "site.preview": "Blender on the CPU · aerial views of the greybox",
    "shots.camera": `Blender · writing ${t.shot || "the"} camera into the .blend`,
    "plan.analyze": `Vision LLM · listing assets in ${base(t.scene) || "the scene"}`,
    "plan.refine": `SAM 3.1 · redrawing the boxes of ${t.plan || "the plan"}`,
    "style.draft": "Vision LLM · drafting style text from the moodboard",
    "style.explore": "Flux on GPU 1 · scenes from the brief",
    "style.derive": `SAM 3 + Flux · objects from ${base(t.scene) || "the scene"}`,
    "3d.trellis": `TRELLIS.2 on GPU 0 · ${t.asset || "asset"}`,
    "cleanup": `Blender · scaling to plan size, setting pivot, decimating · ${t.asset || ""}`,
  }[j.kind] || jobLabel(j);
}

let jobDlg = null;     // {id, win, close}
let jobDlgPending = null;   // a job just started: opens once the next reload lists it
function openJobDialog(id) {
  if (jobDlg && jobDlg.id === id) return;
  const j = data && data.jobs.find(x => x.id === id);
  if (!j) { jobDlgPending = id; return; }
  if (jobDlgPending === id) jobDlgPending = null;
  if (jobDlg) jobDlg.close();
  const [title] = LOADERS[j.kind] || [`${jobLabel(j)}…`];
  const body = document.createElement("div"); body.className = "loader";
  const d = dialog({title, body, className: "loader-window", buttons: [], onClose: () => { if (jobDlg && jobDlg.id === id) jobDlg = null; }});
  d.win.closest(".modal-back").classList.add("loader-back");
  jobDlg = {id, ...d};
  updateJobDialog();
}

function updateJobDialog() {
  if (jobDlgPending != null && data && data.jobs.some(x => x.id === jobDlgPending)) return openJobDialog(jobDlgPending);
  if (!jobDlg || !data) return;
  const j = data.jobs.find(x => x.id === jobDlg.id);
  if (!j) { jobDlg.close(); return; }
  const [title, pic, unit] = LOADERS[j.kind] || [`${jobLabel(j)}…`, "frames", ""];
  const p = jobProgress(j), win = jobDlg.win;
  let line2, bar, buttons;
  if (j.status === "queued") {
    line2 = `Waiting for ${LANE_NAMES[j.lane] || j.lane}: another job is running there.`;
    bar = progressBar({});
  } else if (j.status === "running") {
    const counted = p.total != null && p.done != null;
    line2 = counted ? `${p.done} of ${p.total}${unit ? ` ${unit}` : ""}${p.eta ? ` · ${p.eta}` : ""}` : p.eta || "Working…";
    bar = progressBar(p);
  } else {
    const secs = j.finished && j.created ? Math.round(j.finished - j.created) : null;
    line2 = j.status === "done" ? `Done${secs != null ? ` in ${fmtDur(secs)}` : ""}.` : j.status === "canceled" ? "Canceled." : `Failed: ${j.error || "unknown error"}`;
    bar = progressBar(j.status === "done" ? {done: 1, total: 1} : {done: p.done || 0, total: p.total || 1});
  }
  const active = isActive(j);
  $(".wt", win).textContent = active ? title : j.status === "done" ? title.replace("…", ": done") : j.status === "canceled" ? title.replace("…", ": canceled") : title.replace("…", ": failed");
  const body = $(".loader", win);
  if (body.dataset.pic !== pic) { body.dataset.pic = pic; body.innerHTML = `<div class="ld-pic">${PICS[pic] || ""}</div><div class="ld-line1"></div><div class="ld-bar"></div><div class="ld-foot"><span class="ld-line2"></span><span class="ld-btns"></span></div>`; }
  body.classList.toggle("still", !active);
  $(".ld-line1", body).textContent = detailLine(j, p);
  $(".ld-bar", body).innerHTML = bar;
  const l2 = $(".ld-line2", body); l2.textContent = line2; l2.className = `ld-line2${j.status === "error" ? " error" : ""}`;
  const btns = $(".ld-btns", body);
  const want = active ? (STOPPABLE(j.kind) ? "cancel" : "none") : "close";
  if (btns.dataset.mode !== want) {
    btns.dataset.mode = want; btns.replaceChildren();
    const b = document.createElement("button"); b.type = "button";
    if (want === "cancel") { b.textContent = "Cancel"; b.onclick = () => { b.disabled = true; b.textContent = "Canceling…"; cancelJobs([j]); }; }
    else if (want === "close") { b.textContent = "Close"; b.className = "default"; b.onclick = () => jobDlg && jobDlg.close(); }
    else { b.textContent = "Hide"; b.title = "Analysis can't be interrupted mid-request"; b.onclick = () => jobDlg && jobDlg.close(); }
    btns.append(b);
    if (want === "close") b.focus();
  }
}
