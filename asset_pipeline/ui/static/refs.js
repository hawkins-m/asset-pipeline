// Stage 2 (References): reference sheets per plan unit, generated in the background.
const fmtDims = d => `${d.width}×${d.depth}×${d.height} m`;
// Reroll guidance being typed, per unit ("plan/key"): kept across re-renders (the list redraws
// while jobs run) until the reroll sends it; the field starts from the last batch's guidance.
const refsGuidance = {};

function renderRefs() {
  const list = $("#refs-list");
  const focused = document.activeElement && document.activeElement.classList.contains("guidance")
    ? {key: document.activeElement.dataset.unit, a: document.activeElement.selectionStart, b: document.activeElement.selectionEnd} : null;
  list.replaceChildren();
  const missingJobs = activeJobs("refs.generate", {missing: true});
  const missing = refs.filter(u => !u.sheets.length).length;
  $("#refs-missing").disabled = !!missingJobs.length || !missing;
  $("#refs-missing").textContent = missingJobs.length ? "Generating…" : `Generate missing (${missing})`;
  setStop("#refs-missing-stop", missingJobs);
  const starred = refs.reduce((k, u) => k + u.sheets.filter(s => data.stars[s]).length, 0);
  $("#refs-summary").textContent = refs.length ?
    `${refs.length} assets, ${refs.length - missing} with sheets, ${starred} sheet(s) starred.` :
    "No included assets yet: analyse a scene in Library › Scene plans.";

  for (const plan of [...new Set(refs.map(u => u.plan))]) {
    const sec = document.createElement("section"); sec.className = "refs-plan";
    sec.innerHTML = `<h3>${esc(plan)}</h3>`;
    for (const u of refs.filter(x => x.plan === plan)) {
      const a = u.assets[0];
      const el = document.createElement("div"); el.className = "unit";
      const what = u.kind === "kit" ? u.assets.map(x => esc(x.name)).join(", ") :
        `×${a.count} · ${fmtDims(a.dimensions)}`;
      el.innerHTML = `<div class="unit-head">
          <span class="title">${esc(u.title)}</span><span class="chip">${u.kind}</span>
          <span class="hint">${what}</span>
          <input type="text" class="guidance" maxlength="500" aria-label="Guidance for ${esc(u.title)}'s next sheets"
            placeholder="guidance for the next sheets (optional)" title="Added to this asset's prompt for the next batch only, and saved with those sheets">
          <button type="button" class="secondary more">+ ${+$("#refs-n").value || 2} more</button>
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
      u.sheets.forEach(k => {
        const m = (u.sheet_meta || {})[k] || {};
        const c = card(k, {label: m.guidance ? `<span class="guided" title="${esc(m.guidance)}">“${esc(m.guidance)}”</span>` : ""});
        g.append(c);
      });
      el.append(g);
      $(".more", el).onclick = async () => {
        const guidance = gIn.value.trim();
        if (await generateRefs({plan: u.plan, unit: u.key, guidance: guidance || null})) delete refsGuidance[ukey];
      };
      sec.append(el);
    }
    list.append(sec);
  }
  if (focused) {                       // keep typing where you were through a re-render
    const inp = [...list.querySelectorAll("input.guidance")].find(x => x.dataset.unit === focused.key);
    if (inp && !inp.disabled) { inp.focus(); inp.setSelectionRange(focused.a, focused.b); }
  }
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
  body.innerHTML = `<p>${nx.has_brief ? `<b>${esc(u.title)}</b> already has a brief. Re-brief from <code>${esc(sheet.split("/").pop())}</code>
    replaces it (your feature edits stay; the measured proportions and the view change), or just rebuild with the brief as it is.`
      : `Brief <b>${esc(u.title)}</b> from <code>${esc(sheet.split("/").pop())}</code> with the vision model, then build it.`}</p>
    <div class="form"><label for="bb-view">View to measure:</label><select id="bb-view">
      <option value="front">Auto (the most frontal view)</option><option value="0">Left view</option>
      <option value="1">Middle view</option><option value="2">Right view</option><option value="all">The whole sheet</option></select>
      <div id="bb-form" style="display:contents"></div></div>`;
  const info = nx.form || {form: null, suggested: "cube", size_m: null};
  try { $("#bb-form", body).innerHTML = await formFieldsHtml(info); formFieldsWire(body); }
  catch (err) { $("#bb-form", body).innerHTML = `<p class="error">${esc(errorText(err))}</p>`; }
  if (!info.form || !info.size_m) body.insertAdjacentHTML("afterbegin",
    `<p class="hint">${esc(u.title)} has no ${!info.form && !info.size_m ? "building form or size" : !info.form ? "building form" : "size"} yet:
      choose ${!info.form ? "the form" : "the size"} below. It is saved with the entry (Manage entry… › Form).</p>`);
  const go = async rebrief => {
    const view = $("#bb-view", body).value;
    let ff = {form: null, dims: null};
    if (rebrief) {
      try { ff = formFieldsRead(body); } catch (err) { alert(errorText(err)); return false; }
      if (!ff.dims && !info.size_m) { alert("Size: fill in width, depth and height (m) for the brief."); return false; }
      if (ff.form === info.form) ff.form = null;                  // unchanged: theirs stays theirs
      if (ff.dims && JSON.stringify(ff.dims) === JSON.stringify(info.size_m)) ff.dims = null;
    }
    try { await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(u.key)}/brief_build`,
                    {sheet, rebrief, view, form: ff.form, dims: ff.dims}); await load(); }
    catch (err) { alert(errorText(err)); }
  };
  dialog({title: "Brief & build", body, buttons: nx.has_brief ? [
    {label: "Re-brief from this sheet", default: true, onClick: () => go(true)},
    {label: "Just rebuild", onClick: () => go(false)},
    {label: "Cancel"}] : [{label: "Brief & build", default: true, onClick: () => go(true)}, {label: "Cancel"}]});
}
