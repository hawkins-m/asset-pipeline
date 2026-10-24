// Doctor (Tools › Doctor…, and the line in the Status box): the same checks as `ap doctor`. A
// feature whose piece is missing is switched off with the reason as its tooltip: menu actions
// through ACTION_NEEDS, buttons in the tabs through a data-needs="feature [feature]" attribute
// (kept off across re-renders), and the Unreal tab says why at its top. Server: ui/setup_api.py.

let DOCTOR = null;

// menu and toolbar actions -> the features they need (doctor.FEATURES)
const ACTION_NEEDS = {
  applyAll: ["blender"], build: ["blender"], render: ["blender"], previews: ["blender"], exportUe: ["blender"],
  frames: ["comfy"], framesGenerate: ["comfy"],
  ueInit: ["unreal"], ueImport: ["unreal"], ueRender: ["unreal"], ueLook: ["unreal"], uePull: ["unreal"], ueStale: ["unreal"],
};

// null when every feature in `feats` is available, else the first missing one's reason
function needsWhy(feats) {
  if (!DOCTOR || !DOCTOR.features) return null;
  for (const f of feats) {
    const s = DOCTOR.features[f];
    if (s && !s.ok) return `${s.what} is off. ${s.why}`;
  }
  return null;
}

function applyNeeds(root = document) {
  root.querySelectorAll("[data-needs]").forEach(el => {
    const why = needsWhy(el.dataset.needs.split(/\s+/));
    if (why) {
      if (!el.disabled) el.disabled = true;
      if (el.title !== why) { if (!el.dataset.needsTitle) el.dataset.needsTitle = el.title || " "; el.title = why; }
      el.dataset.needsOff = "1";
    } else if (el.dataset.needsOff) {                 // fixed since: give the element back to its own logic
      delete el.dataset.needsOff;
      el.title = (el.dataset.needsTitle || "").trim(); delete el.dataset.needsTitle;
      el.disabled = false;
      if (typeof renderShell === "function" && typeof data !== "undefined" && data) renderShell();
    }
  });
  const note = $("#unreal-missing");
  if (note) {
    const why = needsWhy(["unreal"]);
    note.hidden = !why;
    if (why) note.innerHTML = `<b>Unreal Engine isn't set up</b>, so the steps here are off; everything else in the app works.
      ${esc(DOCTOR.features.unreal.why)} <button type="button" class="small" data-open="setup">Setup…</button>`;
  }
}

// tab scripts re-render their buttons and set disabled themselves: put the missing-piece state back
const needsObserver = new MutationObserver(() => { if (DOCTOR) requestAnimationFrame(() => applyNeeds()); });

function wrapActions() {
  for (const [name, feats] of Object.entries(ACTION_NEEDS)) {
    const a = ACTIONS[name];
    if (!a || a._needs) continue;
    const enabled = a.enabled, title = a.title;
    a._needs = feats;
    a.enabled = () => !needsWhy(feats) && (enabled ? enabled() : true);
    Object.defineProperty(a, "title", {get: () => needsWhy(feats) || title, configurable: true});
  }
}

function renderDoctorLine() {
  const box = $("#doctor-line");
  if (!box) return;
  if (!DOCTOR || DOCTOR.pending) { box.innerHTML = `<span class="hint" style="margin:0">Checking this machine…</span>`; return; }
  const off = Object.entries(DOCTOR.features).filter(([, v]) => !v.ok).map(([, v]) => v.what.split(" (")[0]);
  const msg = DOCTOR.missing ? `${DOCTOR.missing} missing` : DOCTOR.optional_missing ? "ready; some extras off" : "all set";
  box.innerHTML = `<span class="${DOCTOR.missing ? "error" : ""}" title="${esc(off.length ? `Off: ${off.join("; ")}` : "Every check passed")}">Setup: ${esc(msg)}</span>
    <button type="button" class="small" data-open="doctor">Details…</button>`;
}

async function fetchDoctor(refresh = false) {
  try {
    const r = await api(`/api/doctor${refresh ? "?refresh=1" : ""}`, undefined, "GET");
    if (r.pending) { DOCTOR = DOCTOR || r; renderDoctorLine(); setTimeout(() => fetchDoctor(), 2000); return DOCTOR; }
    DOCTOR = r;
  } catch (_) { return DOCTOR; }                     // the app works without it
  renderDoctorLine();
  applyNeeds();
  return DOCTOR;
}

async function doctorDialog() {
  const body = document.createElement("div"); body.id = "doctor-body";
  const d = dialog({title: "Doctor", body, className: "tool-window doctor-window", buttons: [
    {label: "Check again", onClick: () => { draw(true); return false; }},
    {label: "Setup…", onClick: () => { d.close(); setupDialog(); return false; }},
    {label: "Close", default: true}]});
  d.win.id = "doctor-dialog";
  const draw = async refresh => {
    body.innerHTML = `<p class="hint">Checking…</p>`;
    const r = await fetchDoctor(refresh);
    if (!r || r.pending) { body.innerHTML = `<p class="hint">Still checking; try again in a moment.</p>`; return; }
    const off = Object.values(r.features).filter(v => !v.ok);
    body.innerHTML = `<table class="grid-table"><tbody>${r.checks.map((c, i) => `<tr data-check="${esc(c.id)}">
        <td class="${c.level === "ok" ? "ok" : c.level === "fail" && !c.optional ? "error" : "su-warn"}">${c.level === "ok" ? "ok" : c.level === "warn" ? "note" : c.optional ? "off" : "missing"}</td>
        <td><b>${esc(c.name)}</b><br><span class="hint" style="margin:0">${esc(c.detail)}</span>
          ${c.fix ? `<br>Fix: <code>${esc(c.fix)}</code> <button type="button" class="small dr-copy" data-i="${i}">Copy</button>` : ""}</td></tr>`).join("")}</tbody></table>
      ${off.length ? `<p><b>Off until fixed:</b> ${off.map(v => esc(v.what)).join("; ")}.</p>` : ""}
      <p><b>${r.missing ? `${r.missing} required piece${r.missing === 1 ? " is" : "s are"} missing.` : "Everything required is in place."}</b>
        <span class="hint">Same as <code>ap doctor</code>; checked in ${r.seconds} s.</span></p>`;
    body.querySelectorAll(".dr-copy").forEach(b => b.onclick = async () => {
      b.textContent = (await copyText(r.checks[+b.dataset.i].fix)) ? "Copied" : "Copy failed";
    });
  };
  draw(false);
}

ACTIONS.doctor = {run: doctorDialog};
{
  const tools = MENUS.find(m => m[0] === "Tools")[2];
  tools.splice(tools.findIndex(x => x[1] === "setup") + 1, 0, ["Doctor…", "doctor"]);
}
document.addEventListener("click", e => {               // buttons inside rendered text: Setup… / Details…
  const b = e.target.closest("[data-open]");
  if (!b) return;
  if (b.dataset.open === "setup") setupDialog();
  if (b.dataset.open === "doctor") doctorDialog();
});

window.addEventListener("load", () => {
  wrapActions();                                       // world.js adds its actions before this runs
  needsObserver.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ["disabled"]});
  fetchDoctor();
});
