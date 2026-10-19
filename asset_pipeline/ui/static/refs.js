// Stage 2 (References): reference sheets per plan unit, generated in the background.
const fmtDims = d => `${d.width}×${d.depth}×${d.height} m`;
// Reroll guidance being typed, per unit ("plan/key"): kept across re-renders (the list redraws
// while jobs run) until the reroll sends it; the field starts from the last batch's guidance.
const refsGuidance = {};
// sheets ticked for "Delete selected", by sheet key (kept across re-renders)
const refsSel = {};

function renderRefs() {
  const list = $("#refs-list");
  const focused = document.activeElement && document.activeElement.classList.contains("guidance")
    ? {key: document.activeElement.dataset.unit, a: document.activeElement.selectionStart, b: document.activeElement.selectionEnd} : null;
  const missingJobs = activeJobs("refs.generate", {missing: true});
  const missing = refs.filter(u => !u.sheets.length).length;
  $("#refs-missing").disabled = !!missingJobs.length || !missing;
  $("#refs-missing").textContent = missingJobs.length ? "Generating…" : `Generate missing (${missing})`;
  setStop("#refs-missing-stop", missingJobs);
  const starred = refs.reduce((k, u) => k + u.sheets.filter(s => data.stars[s]).length, 0);
  $("#refs-summary").textContent = refs.length ?
    `${refs.length} assets, ${refs.length - missing} with sheets, ${starred} sheet(s) starred.` :
    "No included assets yet: analyse a scene in Library › Scene plans.";

  // keyed: a unit is rebuilt only when its sheets, stars or job changed (keyed.js), so sheets never reload
  const plansR = [...new Set(refs.map(u => u.plan))];
  keyedSections(list, plansR.map(p => ({key: p, items: refs.filter(x => x.plan === p)})), {
    cls: "refs-plan",
    heading: g => `<h3>${esc(g.key)}</h3>`,
    key: u => `${u.plan}/${u.key}`,
    sig: u => JSON.stringify([u, u.sheets.map(k => !!data.stars[k]), fileVersion, +$("#refs-n").value || 2,
      activeJobs("refs.generate", {plan: u.plan, unit: u.key}).map(j => j.status)]),
    build: refsUnit,
  });
  if (focused) {                       // keep typing where you were through a re-render
    const inp = [...list.querySelectorAll("input.guidance")].find(x => x.dataset.unit === focused.key);
    if (inp && !inp.disabled) { inp.focus(); inp.setSelectionRange(focused.a, focused.b); }
  }
}

// one asset's row: its head (title, guidance, + more), next step and sheets
function refsUnit(u) {
  const a = u.assets[0];
  const el = document.createElement("div"); el.className = "unit"; el.dataset.unit = u.key;
  const what = u.kind === "kit" ? u.assets.map(x => esc(x.name)).join(", ") :
    `×${a.count} · ${fmtDims(a.dimensions)}`;
  el.innerHTML = `<div class="unit-head">
      <span class="title">${u.plan === "library" ? `<a href="#" class="jump-link" data-jump="assets" data-asset="${esc(u.key)}" title="Show it in Asset review">${esc(u.title)}</a>` : esc(u.title)}</span><span class="chip">${u.kind}</span>
      <span class="hint">${what}</span>
      <input type="text" class="guidance" maxlength="500" aria-label="Guidance for ${esc(u.title)}'s next sheets"
        placeholder="guidance for the next sheets (optional)" title="Added to this asset's prompt for the next batch only, and saved with those sheets">
      <button type="button" class="secondary more">+ ${+$("#refs-n").value || 2} more</button>
      <button type="button" class="small sheets-del-sel" hidden>Delete selected</button>
    </div>`;
  const ukey = `${u.plan}/${u.key}`, gIn = $(".guidance", el);
  gIn.dataset.unit = ukey;
  gIn.value = refsGuidance[ukey] ?? u.guidance ?? "";
  gIn.oninput = () => { refsGuidance[ukey] = gIn.value; };
  gIn.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); $(".more", el).click(); } };
  const unitJobs = activeJobs("refs.generate", {plan: u.plan, unit: u.key});
  if (unitJobs.length) {
    const more = $(".more", el);
    more.disabled = true; more.textContent = unitJobs[0].status === "queued" ? "Queued…" : "Generating…";
    gIn.disabled = true;
    more.after(stopButton(unitJobs));
  }
  refsNextStep(u, el);
  const g = document.createElement("div"); g.className = "grid sheets";
  if (!u.sheets.length) g.innerHTML = `<span class="empty">No sheets yet.</span>`;
  const delSel = $(".sheets-del-sel", el);
  const selOf = () => u.sheets.filter(k => refsSel[k]);
  const showSel = () => { const n = selOf().length; delSel.hidden = !n; delSel.textContent = `Delete selected (${n})`; };
  u.sheets.forEach(k => {
    const m = (u.sheet_meta || {})[k] || {};
    const c = card(k);                       // what it was drawn from: sheetCaption below
    const name = k.split("/").pop();
    const pick = document.createElement("label"); pick.className = "sheet-pick";
    pick.innerHTML = `<input type="checkbox" aria-label="Select ${esc(name)}"${refsSel[k] ? " checked" : ""}>`;
    $("input", pick).onchange = e => { if (e.target.checked) refsSel[k] = true; else delete refsSel[k]; showSel(); };
    const del = document.createElement("button"); del.type = "button"; del.className = "sheet-del"; del.textContent = "Delete";
    del.title = `Delete ${name}`;
    del.onclick = () => deleteSheets([k]);
    c.append(pick, del, sheetCaption(m));
    g.append(c);
  });
  delSel.onclick = () => deleteSheets(selOf());
  showSel();
  el.append(g);
  $(".more", el).onclick = async () => {
    const guidance = gIn.value.trim();
    if (await generateRefs({plan: u.plan, unit: u.key, guidance: guidance || null})) delete refsGuidance[ukey];
  };
  return el;
}

// what a sheet was drawn from: the description and guidance it was made with (older sheets
// recorded only their prompt)
function sheetCaption(m) {
  const cap = document.createElement("div"); cap.className = "sheet-cap";
  const cut = (t, n) => t.length > n ? t.slice(0, n - 1) + "…" : t;
  cap.innerHTML = m.description
    ? `<span title="${esc(m.description)}"><b>Description${m.description_from === "prompt" ? " (from its prompt)" : ""}:</b> ${esc(cut(m.description, 110))}</span>`
    : `<span class="hint" title="${esc(m.prompt || "")}">Description not recorded (older sheet)${m.prompt ? ": hover for its prompt" : ""}</span>`;
  if (m.guidance) cap.innerHTML += `<span title="${esc(m.guidance)}"><b>Guidance:</b> “${esc(cut(m.guidance, 80))}”</span>`;
  return cap;
}

// Delete sheets after a confirmation that names what still uses them (star, brief, version).
async function deleteSheets(keys) {
  if (!keys.length) return;
  let uses;
  try { uses = (await api(`/api/projects/${slug}/refs/sheets/uses`, {sheets: keys})).uses; }
  catch (err) { alert(errorText(err)); return; }
  const used = keys.filter(k => (uses[k] || []).length);
  const short = k => k.split("/").slice(-2).join("/");
  const body = `<p>Delete ${keys.length === 1 ? `<b>${esc(short(keys[0]))}</b>` : `${keys.length} sheets (${keys.map(k => esc(short(k))).join(", ")})`}?
      Views already cut from ${keys.length === 1 ? "it" : "them"} stay.</p>
    ${used.length ? `<p><b>Still in use:</b></p><ul id="sheet-uses">${used.map(k => `<li><b>${esc(short(k))}</b>: ${uses[k].map(esc).join("; ")}</li>`).join("")}</ul>
      <p class="hint">Deleting drops its star. A brief or version that names it keeps its build, but its source sheet is gone.</p>` : ""}
    <p class="hint">The sheet's reference is removed; the file itself only when nothing else (a duplicate entry) references it.</p>`;
  if (!await confirmDialog({title: keys.length === 1 ? "Delete sheet" : "Delete sheets", body, danger: true,
                            button: used.length ? "Delete anyway" : "Delete", id: "sheet-delete-dialog"})) return;
  try { await api(`/api/projects/${slug}/refs/sheets/delete`, {sheets: keys, confirm: used.length > 0}); }
  catch (err) { alert(errorText(err)); }
  keys.forEach(k => delete refsSel[k]);
  await load();
}

async function generateRefs(body) {
  try {
    await api(`/api/projects/${slug}/refs/generate`, {...body, n: +$("#refs-n").value || 2});
    await load();
    return true;
  } catch (err) { alert(errorText(err)); return false; }
}

$("#refs-missing").onclick = () => generateRefs({});

// Next step once a sheet of a library asset is starred: procedural assets get Brief & build (the
// brief from the sheet, then the Blender build and review renders), TRELLIS ones Cut views.
// With several starred sheets the newest one (highest sheet number) is used; the title says which.
function refsNextStep(u, el) {
  const nx = u.next; if (!nx) return;
  const starred = u.sheets.filter(s => data.stars[s]);
  if (!starred.length) return;
  const sheet = starred.at(-1), short = sheet.split("/").pop();
  const box = document.createElement("div"); box.className = "next-step"; box.dataset.unit = u.key;
  const jobKind = nx.route === "procedural" ? "library.brief_build" : "views.cut";
  const running = nx.route === "procedural" ? activeJobs(jobKind, {asset: u.key}) : activeJobs(jobKind, {sheet});
  const btn = document.createElement("button"); btn.type = "button"; btn.className = "next-btn";
  if (nx.route === "procedural") {
    btn.textContent = running.length ? "Building…" : "Brief & build"; btn.classList.add("next-brief");
    btn.title = `Brief from ${short}${starred.length > 1 ? ` (the newest of ${starred.length} starred sheets)` : ""}, then build in Blender and render the review`;
    btn.onclick = () => refsBriefBuild(u, sheet, nx);
  } else {
    btn.textContent = running.length ? "Cutting…" : "Cut views"; btn.classList.add("next-cut");
    btn.title = `Cut the views of ${short}${starred.length > 1 ? ` (the newest of ${starred.length} starred sheets)` : ""} for TRELLIS`;
    btn.onclick = async () => {
      try { await api(`/api/projects/${slug}/views/cut`, {sheet}); await load(); } catch (err) { alert(errorText(err)); }
    };
  }
  btn.disabled = !!running.length;
  box.append(btn);
  if (nx.route !== "procedural") {
    const h = document.createElement("span"); h.className = "hint guide-hint"; h.style.margin = "0 6px";
    h.append("Text steering for TRELLIS assets happens on the sheet: ");
    const a = document.createElement("a"); a.href = "#"; a.textContent = "Guidance on the sheet reroll (Library › Reference sheets › +2 more)";
    a.onclick = ev => { ev.preventDefault(); showTab("library"); showSub("refs"); };
    h.append(a); box.append(h);
  }
  if (running.length) box.append(stopButton(running));
  if (nx.route === "procedural" && nx.has_build) {
    const a = document.createElement("a"); a.href = `/?project=${encodeURIComponent(slug)}&review=${encodeURIComponent(u.key)}`;
    a.className = "next-open"; a.textContent = "Open in Asset review";
    a.onclick = ev => { ev.preventDefault(); showTab("library"); showSub("assets"); openAssetReview(u.key); };
    box.append(" ", a);
  } else if (nx.route !== "procedural" && nx.views > 0) {
    const a = document.createElement("a"); a.href = "#"; a.className = "next-views";
    a.textContent = "Choose view in Views and 3D";
    a.onclick = ev => { ev.preventDefault(); showTab("views"); };
    box.append(" ", a);
  }
  $(".unit-head", el).append(box);
}

async function refsBriefBuild(u, sheet, nx) {
  // The view the brief reads proportions and rhythm from: auto picks the most frontal one,
  // which fails on curved buildings (vaults); then choose it by eye.
  const body = document.createElement("div");
  body.innerHTML = `<p>${nx.has_brief ? `<b>${esc(u.title)}</b> already has a brief. <b>Re-brief from this sheet</b> measures
    <code>${esc(sheet.split("/").pop())}</code> again and keeps your edits; <b>Re-brief from scratch</b> drops them and takes only what this
    sheet shows. Both show the fields before building. Or just rebuild with the brief as it is.`
      : `Brief <b>${esc(u.title)}</b> from <code>${esc(sheet.split("/").pop())}</code> with the vision model, then build it.`}</p>
    <div class="form"><label for="bb-view">View to measure:</label><select id="bb-view">
      <option value="front">Auto (the most frontal view)</option><option value="0">Left view</option>
      <option value="1">Middle view</option><option value="2">Right view</option><option value="all">The whole sheet</option></select>
      <div id="bb-form" style="display:contents"></div>
      <label for="bb-guidance">Guidance for the next build:</label>
      <textarea id="bb-guidance" rows="3" placeholder="Optional. What should change, in words (taller windows, an open arcade on the ground floor…). The vision model turns it into brief edits you accept before the build."></textarea></div>`;
  $("#bb-guidance", body).value = nx.guidance || "";
  const info = nx.form || {form: null, suggested: "cube", size_m: null};
  try { $("#bb-form", body).innerHTML = await formFieldsHtml(info); formFieldsWire(body); }
  catch (err) { $("#bb-form", body).innerHTML = `<p class="error">${esc(errorText(err))}</p>`; }
  if (!info.form || !info.size_m) body.insertAdjacentHTML("afterbegin",
    `<p class="hint">${esc(u.title)} has no ${!info.form && !info.size_m ? "building form or size" : !info.form ? "building form" : "size"} yet:
      choose ${!info.form ? "the form" : "the size"} below. It is saved with the entry (Manage entry… › Form).</p>`);
  const go = async (rebrief, scratch = false) => {
    const view = $("#bb-view", body).value;
    const guidance = $("#bb-guidance", body).value.trim();
    let ff = {form: null, dims: null};
    if (rebrief) {
      try { ff = formFieldsRead(body); } catch (err) { alert(errorText(err)); return false; }
      if (!ff.dims && !info.size_m) { alert("Size: fill in width, depth and height (m) for the brief."); return false; }
      if (ff.form === info.form) ff.form = null;                  // unchanged: theirs stays theirs
      if (ff.dims && JSON.stringify(ff.dims) === JSON.stringify(info.size_m)) ff.dims = null;
    }
    if (guidance) {                        // propose edits first; the build starts when they are accepted
      guideRun(u.key, sheet, view, guidance, load);
      return true;
    }
    const send = async extra => {
      try { await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(u.key)}/brief_build`,
                      {sheet, rebrief, view, form: ff.form, dims: ff.dims, ...extra}); await load(); }
      catch (err) { alert(errorText(err)); }
    };
    if (rebrief && nx.has_brief) { setTimeout(() => rebriefFields(u, sheet, scratch, info, send), 0); return true; }
    await send({});
  };
  dialog({title: "Brief & build", body, buttons: nx.has_brief ? [
    {label: "Re-brief from this sheet (keep my edits)", default: true, onClick: () => go(true)},
    {label: "Re-brief from scratch", onClick: () => go(true, true)},
    {label: "Just rebuild", onClick: () => go(false)},
    {label: "Cancel"}] : [{label: "Brief & build", default: true, onClick: () => go(true)}, {label: "Cancel"}]});
}

// Re-brief, second step: what the sheet gives (measured again) and the edits carried over, each
// with a tick box (untick to drop it). From scratch starts with none ticked.
async function rebriefFields(u, sheet, scratch, info, send) {
  let src;
  try { src = await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(u.key)}/brief/sources`, undefined, "GET"); }
  catch (err) { alert(errorText(err)); return; }
  const short = sheet.split("/").pop(), otherSheet = src.sheet_name && src.sheet_name !== short;
  const val = v => v == null ? "off" : Array.isArray(v) ? (v.every(x => typeof x !== "object") ? v.join(", ") : JSON.stringify(v))
    : typeof v === "object" ? JSON.stringify(v) : String(v);
  const tickedByDefault = e => !scratch && !(otherSheet && e.path.startsWith("patches."));
  const body = document.createElement("div"); body.id = "rebrief-body"; body.className = "rebrief";
  body.innerHTML = `<p style="margin-top:0">${scratch ? `<b>From scratch:</b> nothing below is kept unless you tick it.` : `<b>Keeping your edits:</b> untick any to drop it.`}
      Form <b>${esc(info.form || "–")}</b>${info.source ? ` (${esc(info.source)})` : ""} and size
      <b>${info.size_m ? info.size_m.map(x => +(+x).toFixed(1)).join(" × ") + " m" : "–"}</b>${info.size_source ? ` (${esc(info.size_source)})` : ""} come from the entry.</p>
    <fieldset><legend>From the sheet · measured again on <code>${esc(short)}</code></legend>
      <table class="grid-table rb-sheet"><thead><tr><th>Field</th><th>Now${src.sheet_name ? ` (${esc(src.sheet_name)})` : ""}</th><th></th></tr></thead><tbody>
      ${src.sheet.map(r => `<tr data-over="${esc(r.overridden_by || "")}"><td>${esc(r.label)}</td><td class="mono">${esc(Array.isArray(r.value) ? "box " + r.value.join(", ") : r.value == null ? "not measured" : val(r.value))}</td>
        <td class="hint rb-over"></td></tr>`).join("")}</tbody></table></fieldset>
    <fieldset><legend>Your edits · carried over</legend>
      ${src.edits.length ? `<div class="row" style="margin:0 0 4px"><button type="button" class="small" data-all="1">Tick all</button>
          <button type="button" class="small" data-all="0">Untick all</button><span class="hint" id="rb-count" style="margin:0"></span></div>
        <table class="grid-table rb-edits"><thead><tr><th></th><th>Field</th><th>Your value</th></tr></thead><tbody>
        ${src.edits.map((e, i) => `<tr><td><input type="checkbox" id="rb-${i}" data-path="${esc(e.path)}" ${tickedByDefault(e) ? "checked" : ""}></td>
          <td><label for="rb-${i}">${esc(e.label)}</label>${otherSheet && e.path.startsWith("patches.") ? ` <span class="hint" style="margin:0">(a box on the old view)</span>` : ""}</td>
          <td class="mono">${esc(val(e.value))}</td></tr>`).join("")}</tbody></table>`
        : `<p class="hint" style="margin:0">None: the brief has no edits of yours.</p>`}</fieldset>
    <p class="hint">Everything else comes from the builder's defaults for the form.</p>`;
  const sync = () => {
    const boxes = [...body.querySelectorAll("input[data-path]")], on = new Set(boxes.filter(b => b.checked).map(b => b.dataset.path));
    const c = $("#rb-count", body); if (c) c.textContent = `${on.size} of ${boxes.length} kept`;
    body.querySelectorAll(".rb-sheet tr[data-over]").forEach(tr => {
      const o = tr.dataset.over, kept = o && on.has(o);
      tr.classList.toggle("over", !!kept);
      $(".rb-over", tr).textContent = !o ? "from the sheet" : kept ? `your edit (${o.split(".").at(-1).replace(/_/g, " ")}) wins` : "from the sheet (your edit dropped)";
    });
  };
  body.addEventListener("change", sync);
  body.querySelectorAll("[data-all]").forEach(b => b.onclick = () => { body.querySelectorAll("input[data-path]").forEach(x => x.checked = b.dataset.all === "1"); sync(); });
  sync();
  const d = dialog({title: `Re-brief ${u.title}`, body, className: "tool-window", buttons: [
    {label: "Brief & build", default: true, onClick: async () => {
      const boxes = [...body.querySelectorAll("input[data-path]")];
      const drop = boxes.filter(b => !b.checked).map(b => b.dataset.path);
      await send({scratch: boxes.every(b => !b.checked), drop});
    }}, {label: "Cancel"}]});
  d.win.id = "rebrief-dialog";
}

// --- guidance for the next build (procedural assets) ------------------------------------------
// The vision model proposes brief edits from the guidance; the artist accepts rows, then the
// accepted edits and the guidance go into the brief and the asset rebuilds. Also used by Asset review.
const guideLabel = p => p.replace(/^features\./, "").split(".").join(" › ").replace(/_/g, " ");
const guideShow = v => v === null || v === undefined ? "(not set)" : Array.isArray(v) ? v.join(", ") : String(v);
function guideParse(text, before) {            // the edited text back to the kind of the field
  const t = text.trim();
  if (Array.isArray(before)) return t.split(",").map(x => x.trim()).filter(Boolean).map(x => before.every(y => typeof y === "number") && !isNaN(+x) ? +x : x);
  if (typeof before === "boolean") return t.toLowerCase() === "true";
  if (typeof before === "number" || before === null) return t !== "" && !isNaN(+t) ? +t : t;
  return t;
}

async function guideRun(asset, sheet, view, guidance, onDone) {
  let job;
  try { job = await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(asset)}/guide`, {sheet, guidance, view}); }
  catch (err) { alert(errorText(err)); return; }
  for (;;) {                                    // the proposal arrives as the job's result
    await new Promise(r => setTimeout(r, 1000));
    try { job = await api(`/api/jobs/${job.id}`, undefined); } catch (err) { alert(errorText(err)); return; }
    if (!["queued", "running"].includes(job.status)) break;
  }
  if (typeof jobDlg !== "undefined" && jobDlg) jobDlg.close();
  if (job.status !== "done") { if (job.status !== "canceled") alert(job.error || "The vision model did not answer."); return; }
  guideEdits(asset, guidance, job.result, onDone);
}

function guideEdits(asset, guidance, res, onDone) {
  const body = document.createElement("div"); body.id = "gd-edits";
  const rows = res.edits.map((e, i) => `<tr class="gd-row" data-path="${esc(e.path)}" data-i="${i}">
      <td><input type="checkbox" class="gd-accept" checked aria-label="Accept ${esc(guideLabel(e.path))}"></td>
      <td title="${esc(e.path)}">${esc(guideLabel(e.path))}</td><td class="gd-before"${e.switch_on ? ` title="${esc(guideLabel(e.switch_on))} is off: this switches it on (its other fields from the template)"` : ""}>${e.switch_on ? `<i>off</i> (${esc(guideShow(e.before))})` : esc(guideShow(e.before))}</td>
      <td><input class="gd-after" value="${esc(guideShow(e.after))}" aria-label="New value for ${esc(guideLabel(e.path))}"></td>
      <td class="hint" style="margin:0">${esc(e.why)}</td></tr>`).join("");
  body.innerHTML = `<p>${res.brief_made ? "There was no brief yet, so one was made from the sheet first. " : ""}${res.edits.length ?
      "The vision model proposes these changes to the brief. Untick the ones you don't want, or edit the new value." :
      "The vision model found nothing it could express with the brief's fields. The guidance is still saved with the brief."}</p>
    ${res.edits.length ? `<table class="gd-table"><thead><tr><th></th><th>Field</th><th>Before</th><th>After</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
    ${res.dropped.length ? `<details class="gd-dropped"><summary>${res.dropped.length} proposed change${res.dropped.length === 1 ? "" : "s"} left out</summary><ul>${
      res.dropped.map(d => `<li><code>${esc(d.path)}</code>: ${esc(d.reason)}</li>`).join("")}</ul></details>` : ""}`;
  const go = async (confirm = false) => {
    const edits = [...body.querySelectorAll(".gd-row")].filter(r => $(".gd-accept", r).checked).map(r => {
      const e = res.edits[+r.dataset.i];
      return {path: e.path, after: guideParse($(".gd-after", r).value, e.before), why: e.why};
    });
    try { await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(asset)}/guide/apply`, {guidance, edits, confirm}); }
    catch (err) {
      if (!confirm && /locked frames/.test(errorText(err)) && window.confirm(`${errorText(err)}\n\nRebuild anyway?`)) return go(true);
      alert(errorText(err)); return false;
    }
    if (onDone) await onDone();
  };
  dialog({title: "Guidance: proposed changes", body, className: "gd-window", buttons: [
    {label: "Accept & build", default: true, onClick: () => go(false)}, {label: "Cancel"}]});
}
