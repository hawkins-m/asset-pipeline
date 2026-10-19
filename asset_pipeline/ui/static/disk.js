// Project size on disk: the toolbar's "Project: 3.1 GB" and the "<project> Properties" dialog
// (Windows XP Disk Properties style: a 3D pie by category, the drive's used and free space,
// space saved by sharing). The server measures in the background, at most every 30 s
// (asset_pipeline/disk_usage.py); the toolbar asks again after a job finishes.

const DISK_COLOURS = {sheets: "#3A6EA5", views: "#3E9C9E", models: "#B5446E", textures: "#C9822B",
  frames: "#5E8C31", greybox: "#7A5BA6", unreal: "#2F3E73", other: "#6E6E6B"};
const DRIVE_COLOURS = {project: "#3A6EA5", used: "#2F3E73", free: "#B5446E"};
const diskState = {slug: null, fv: 0, res: null, timer: null, dialog: null};
const fmtGB = b => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : fmtBytes(b);
const fmtExact = b => `${Math.round(b || 0).toLocaleString("en")} bytes`;

async function refreshDisk(dirty = false) {
  if (!slug) return;
  const s = slug;
  let r;
  try { r = await api(`/api/projects/${s}/disk${dirty ? "?dirty=true" : ""}`, undefined, "GET"); }
  catch (_) { return; }
  if (s !== slug) return;
  if (r.result) diskState.res = r.result;
  paintDiskButton(r);
  if (diskState.dialog) diskState.dialog(diskState.res, r.pending);
  clearTimeout(diskState.timer);
  diskState.timer = r.pending ? setTimeout(() => refreshDisk(false), 3000) : null;
}

function paintDiskButton(r) {
  const b = $("#tb-disk");
  const res = diskState.res;
  b.textContent = `Project: ${res ? fmtGB(res.total) : "…"}`;
  b.title = res ? `${fmtExact(res.total)} on disk, the Unreal project and its backups included${r && r.pending ? " (updating…)" : ""}. Click for details.`
    : "Measuring the project's size on disk…";
}

// Called by load(): a new project measures at once, a finished job measures again (the server
// keeps it to once every 30 s).
function diskAfterLoad() {
  const b = $("#tb-disk");
  b.hidden = !slug;
  if (!slug) return;
  if (diskState.slug !== slug) {
    Object.assign(diskState, {slug, fv: fileVersion, res: null});
    paintDiskButton(null);
    refreshDisk(false);
  } else if (fileVersion > diskState.fv) {
    diskState.fv = fileVersion;
    refreshDisk(true);
  }
}

// A 3D pie as XP drew it: an ellipse of slices on top of a wall that shows the front half.
function pie3d(slices, {w = 200, h = 120, depth = 18} = {}) {
  const cx = w / 2, cy = (h - depth) / 2, rx = w / 2 - 2, ry = (h - depth) / 2 - 2;
  const total = slices.reduce((a, s) => a + s.value, 0);
  const pt = (a, dy = 0) => `${(cx + rx * Math.cos(a)).toFixed(2)},${(cy + dy + ry * Math.sin(a)).toFixed(2)}`;
  const dark = c => { const n = parseInt(c.slice(1), 16); const f = v => Math.round(v * .62).toString(16).padStart(2, "0");
    return `#${f(n >> 16)}${f((n >> 8) & 255)}${f(n & 255)}`; };
  if (!total) return `<svg class="pie3d" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">
    <ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="var(--field)" stroke="#000" stroke-width=".6"/></svg>`;
  let a = -Math.PI / 2;   // start at the top, clockwise
  const walls = [], tops = [];
  for (const s of slices) {
    if (!s.value) continue;
    const da = s.value / total * Math.PI * 2, a0 = a, a1 = a + da;
    a = a1;
    if (da >= Math.PI * 2 - 1e-6) {
      tops.push(`<ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="${s.colour}" stroke="#000" stroke-width=".6"/>`);
    } else {
      tops.push(`<path d="M${cx},${cy} L${pt(a0)} A${rx},${ry} 0 ${da > Math.PI ? 1 : 0} 1 ${pt(a1)} Z" fill="${s.colour}" stroke="#000" stroke-width=".6"/>`);
    }
    // the front wall is the part of the slice between 0 and pi (screen y points down); a slice can
    // reach it twice when it wraps past 2pi
    for (const off of [0, Math.PI * 2]) {
      const s0 = Math.max(a0, off), s1 = Math.min(a1, off + Math.PI);
      if (s1 - s0 < 1e-6) continue;
      walls.push(`<path d="M${pt(s0)} A${rx},${ry} 0 0 1 ${pt(s1)} L${pt(s1, depth)} A${rx},${ry} 0 0 0 ${pt(s0, depth)} Z"
        fill="${dark(s.colour)}" stroke="#000" stroke-width=".6"/>`);
    }
  }
  return `<svg class="pie3d" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">${walls.join("")}${tops.join("")}</svg>`;
}

const diskLegendRow = (colour, label, bytes, extra = "") => `<tr><td><span class="swatch" style="background:${colour}"></span></td>
  <td>${esc(label)}${extra}</td><td class="num">${fmtExact(bytes)}</td><td class="num">${fmtGB(bytes)}</td></tr>`;

function diskDialog() {
  if (noProject()) return;
  const body = document.createElement("div"); body.id = "disk-body"; body.className = "disk-props";
  const dlg = dialog({title: `${slug} Properties`, body, className: "disk-window", onClose: () => { diskState.dialog = null; },
    buttons: [{label: "OK", default: true}]});
  dlg.win.id = "disk-dialog";
  const draw = (res, pending) => {
    if (!res) { body.innerHTML = `<p class="hint">Measuring the project's size on disk…</p>`; return; }
    const cats = res.categories;
    const ue = res.unreal || {};
    const ueNote = Object.entries(ue).map(([k, u]) => `${k === "project" ? "project" : "backups"} ${fmtGB(u.bytes)}${u.other_drive ? ", other drive" : ""}`).join("; ");
    const d = res.drive;
    const here = res.total - Object.values(ue).filter(u => u.other_drive).reduce((a, u) => a + u.bytes, 0);
    const otherUsed = Math.max(0, d.used - here);
    body.innerHTML = `<div role="tablist" class="disk-tabs"><button role="tab" aria-selected="true" tabindex="-1">General</button></div>
      <div class="disk-page">
        <div class="disk-head"><svg viewBox="0 0 32 32" width="32" height="32" aria-hidden="true"><rect x="2" y="9" width="28" height="15" rx="2" fill="#A7A7A4" stroke="#000"/>
          <rect x="4" y="11" width="24" height="7" fill="#7E7E7B"/><circle cx="25" cy="21" r="1.3" fill="#3C9A3C"/></svg>
          <b>${esc(slug)}</b><span class="hint" style="margin:0 0 0 auto">${pending ? "Updating…" : `Measured in ${res.seconds} s`}</span></div>
        <hr>
        <table class="disk-legend"><tbody>
          ${cats.map(c => diskLegendRow(DISK_COLOURS[c.key], c.label, c.bytes,
            c.key === "unreal" && ueNote ? ` <span class="hint" style="margin:0">(${esc(ueNote)})</span>` : "")).join("")}
          <tr class="sum"><td></td><td>Total on disk</td><td class="num">${fmtExact(res.total)}</td><td class="num"><b>${fmtGB(res.total)}</b></td></tr>
        </tbody></table>
        <div class="disk-pie">${pie3d(cats.map(c => ({value: c.bytes, colour: DISK_COLOURS[c.key]})), {w: 220, h: 128, depth: 20})}
          <div>Project</div></div>
        <p id="disk-saved">Space saved by sharing: <b>${fmtGB(res.saved)}</b> <span class="hint" style="margin:0">(sheets, views and 3D models stored once and linked by every entry and version)</span></p>
        <fieldset class="disk-drive"><legend>Drive ${esc(d.path)}</legend>
          <div class="disk-drive-row">${pie3d([{value: here, colour: DRIVE_COLOURS.project}, {value: otherUsed, colour: DRIVE_COLOURS.used},
              {value: d.free, colour: DRIVE_COLOURS.free}], {w: 110, h: 66, depth: 10})}
            <table class="disk-legend"><tbody>
              ${diskLegendRow(DRIVE_COLOURS.project, "This project", here)}
              ${diskLegendRow(DRIVE_COLOURS.used, "Other used space", otherUsed)}
              ${diskLegendRow(DRIVE_COLOURS.free, "Free space", d.free)}
              <tr class="sum"><td></td><td>Capacity</td><td class="num">${fmtExact(d.total)}</td><td class="num">${fmtGB(d.total)}</td></tr>
            </tbody></table></div></fieldset>
        <div class="row disk-actions"><button type="button" id="disk-details">Storage details…</button>
          <button type="button" id="disk-clean"${res.unreferenced ? "" : " disabled"}>Clean unreferenced files${res.unreferenced ? ` (${res.unreferenced}, ${fmtGB(res.unreferenced_bytes)})` : ""}</button></div>
      </div>`;
    $("#disk-details", body).onclick = () => storageDialog();
    $("#disk-clean", body).onclick = async () => {
      const ok = await confirmDialog({title: "Clean unreferenced files", button: "Clean", danger: true, id: "storage-clean-dialog",
        body: `<p>Remove ${res.unreferenced} stored file${res.unreferenced === 1 ? "" : "s"} nothing references, freeing ${fmtGB(res.unreferenced_bytes)}?</p>
          <p class="hint">Files any sheet, view, build or version still uses are never removed.</p>`});
      if (!ok) return;
      try { await api(`/api/projects/${slug}/storage/clean`, {}); } catch (err) { alert(errorText(err)); }
      refreshDisk(true);
    };
  };
  diskState.dialog = draw;
  draw(diskState.res, true);
  refreshDisk(true);
}

$("#tb-disk").onclick = () => diskDialog();
