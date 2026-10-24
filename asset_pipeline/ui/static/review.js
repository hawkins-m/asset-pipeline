// Stages 3-4 (Review): cut views from starred sheets, choose one per asset, tag
// game / cine / hero, and send it to 3D (TRELLIS.2 on GPU 0).
const USAGES = ["game", "cine", "hero"];
const USAGE_TIPS = {game: "Game use: Clean up decimates and bakes this asset for real time.",
  cine: "Cinematic use: Clean up keeps the full mesh and textures.", hero: "Hero use: full mesh and textures, for the assets seen closest in shots."};
const ACTIVE_TIP = "This version is the one later steps (Cleanup, Unreal look scenes) will use.";
const APPROVE_TIP = "Unreal look scenes only place approved assets. Until then, export uses greybox stand-ins.";

function renderReview() {
  if (!reviewData) return;
  const pending = reviewData.pending_sheets.length;
  const cutJobs = activeJobs("views.cut");
  $("#cut-views").disabled = !pending || !!cutJobs.length;
  $("#cut-views").textContent = cutJobs.length ? "Extracting…" : `Extract reference views (${pending} new sheet${pending === 1 ? "" : "s"})`;
  setStop("#cut-stop", cutJobs);
  const withViews = reviewData.assets.filter(a => a.views.length).length;
  const built = reviewData.assets.filter(a => a.results.length).length;
  const pre = (!reviewData.assets.length || (!reviewData.starred_sheets && !withViews && !built)) && libraryPrereq(true);
  const sumText = pre ? prereqHtml(pre)
    : `${reviewData.starred_sheets} starred sheet(s); ${withViews} of ${reviewData.assets.length} assets have views, ${built} have a 3D model.`;
  if ($("#review-summary").dataset.sig !== sumText) {
    $("#review-summary").dataset.sig = sumText;
    if (pre) { $("#review-summary").innerHTML = sumText; wireGo($("#review-summary")); } else $("#review-summary").textContent = sumText;
  }

  // keyed: only rows whose data changed are rebuilt, keeping their loaded images (keyed.js)
  const plans = [...new Set(reviewData.assets.map(a => a.plan))];
  keyedSections($("#review-list"), plans.map(p => ({key: p, items: reviewData.assets.filter(x => x.plan === p)})), {
    heading: g => `<h3>${esc(g.key)}</h3>`,
    key: a => `${a.plan}/${a.asset.id}`,
    sig: a => JSON.stringify([a, fileVersion, assetName(a.asset.id, a.asset.name), a.plan === "library" ? verRev(a.asset.id) : 0,
      ["3d.trellis", "cleanup"].map(k => activeJobs(k, {plan: a.plan, asset: a.asset.id}).map(j => j.status))]),
    build: assetReview,
  });
}

function assetReview(a) {
  const el = document.createElement("div"); el.className = "asset-review"; el.dataset.asset = a.asset.id;
  const x = a.asset, d = x.dimensions;
  el.innerHTML = `<div class="head">
      ${a.current ? `<span class="cur-version" title="${ACTIVE_TIP}">${a.current.thumb ? `<img alt="" src="${fileUrl(a.current.thumb)}">` : ""}<b>v${a.current.n}</b></span>` : ""}
      <span class="title">${a.plan === "library" ? assetLabelHtml(x.id) : esc(x.name)}</span>
      ${a.plan === "library" && assetEntry(x.id) ? `<button type="button" class="small secondary manage-entry" title="Rename, describe, build method, duplicate (Library › Asset review)">Manage entry…</button>` : ""}
      <span class="hint">×${x.count} · ${d.width}×${d.depth}×${d.height} m${x.kit ? ` · kit ${esc(x.kit)}` : ""}</span>
      <span class="usage" role="group" aria-label="Usage">${USAGES.map(u =>
        `<button type="button" data-u="${u}" aria-pressed="${x.usage === u}" title="${USAGE_TIPS[u]}">${u}</button>`).join("")}</span>
      <span class="actions">${a.reroll ? `<button type="button" class="secondary reroll" aria-expanded="false" title="Generate a new procedural model for this asset. Add guidance to steer it. Seeds only apply to TRELLIS builds.">Reroll</button>` : ""}
        <button type="button" class="make3d" data-needs="${a.procedural ? "blender" : "trellis"}">Make 3D</button>
        ${a.procedural ? "" : `<label class="new-seed" title="Draw a random seed for Make 3D instead of 42 (the seed is recorded with the result). Seeds only apply to TRELLIS builds."><input type="checkbox" class="new-seed-box"> New seed</label>`}
        <button type="button" class="secondary cleanup" title="Scale the Active version to the plan size and set its pivot at the base${x.usage === "game" ? ", then decimate and bake it for game use" : ""}.">Clean up</button>
        ${a.plan === "library" ? `<span class="mark ${a.review === "approved" ? "m-ok" : a.review ? "m-chg" : "m-none"}" title="This asset's review status (also in Library › Asset review).">${esc((a.review || "not reviewed").replace("_", " "))}</span>
          <button type="button" class="secondary approve" ${a.results.length ? `title="${APPROVE_TIP}"` : "disabled title=\"Make 3D first.\""}>Approve</button>
          <button type="button" class="secondary changes" title="Mark this asset as needing another build; add the note in Library › Asset review.">Needs changes</button>` : ""}</span>
    </div>
    ${a.reroll ? `<div class="reroll-form row" hidden><input type="text" class="reroll-guidance" maxlength="500"
        placeholder="Guidance (optional): taller windows, an open arcade…" aria-label="Guidance for the reroll">
      <button type="button" class="small default reroll-go" title="Start the reroll; the new version appears under Versions.">Go</button>
      <button type="button" class="small reroll-cancel" title="Close without rerolling.">Cancel</button>
      <div class="hint reroll-settings" style="margin:0;flex-basis:100%"></div></div>` : ""}`;
  const me = $(".manage-entry", el);
  if (me) me.onclick = () => manageEntry(assetEntry(x.id));
  for (const [cls, status] of [[".approve", "approved"], [".changes", "needs_changes"]]) {
    const b = $(cls, el);
    if (b) b.onclick = async () => {
      try { await api(`/api/projects/${slug}/library/asset/${x.id}/review`, {status, note: ""}); await load(); }
      catch (err) { alert(errorText(err)); }
    };
  }
  el.querySelectorAll(".usage button").forEach(b => b.onclick = async () => {
    try { await api(`/api/projects/${slug}/review/usage`, {plan: a.plan, asset: x.id, usage: b.dataset.u}); await load(); }
    catch (err) { alert(errorText(err)); }
  });

  const g = document.createElement("div"); g.className = "grid views";
  if (!a.views.length) g.innerHTML = `<span class="hint no-views" title="Star a sheet in Library › Reference sheets, then click Extract reference views at the top of this tab.">No reference views yet</span>`;
  for (const v of a.views) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "view" + (v.key === a.chosen ? " chosen" : "");
    b.setAttribute("aria-pressed", v.key === a.chosen);
    b.title = `${v.key === a.chosen ? "Make 3D builds from this view; click to unchoose it." : "Click to build from this view with Make 3D."} ${v.sheet}, view ${v.position}${v.asked ? ` (asked: ${v.asked})` : ""}`;
    b.innerHTML = `<img loading="lazy" alt="" src="${fileUrl(v.key)}">
      <span class="label">${v.key === a.chosen ? "✓ " : ""}${esc(v.sheet.split("/").pop().replace(".png", "").replace("sheet_", "s"))} v${v.position}</span>`;
    b.onclick = async () => {
      try { await api(`/api/projects/${slug}/review/choose`, {plan: a.plan, asset: x.id, view: v.key === a.chosen ? null : v.key}); await load(); }
      catch (err) { alert(errorText(err)); }
    };
    g.append(b);
  }
  el.append(g);

  const jobs3d = activeJobs("3d.trellis", {plan: a.plan, asset: x.id});
  const make = $(".make3d", el);
  make.disabled = !a.chosen || !!jobs3d.length;
  make.title = a.chosen ? "Build a 3D model with TRELLIS from the chosen view; it becomes a new version." : "Choose a reference view first.";
  if (jobs3d.length) {
    make.textContent = jobs3d[0].status === "queued" ? "Queued…" : "Building…";
    make.after(stopButton(jobs3d));
  }
  make.onclick = async () => {
    try { await api(`/api/projects/${slug}/3d`, {plan: a.plan, asset: x.id, mode: $("#trellis-mode").value, new_seed: !!$(".new-seed-box", el)?.checked}); await load(); }
    catch (err) { alert(errorText(err)); }
  };

  const rr = $(".reroll", el);
  if (rr) {
    const form = $(".reroll-form", el), field = $(".reroll-guidance", el);
    const rrJobs = activeJobs("library.reroll", {asset: x.id});
    rr.disabled = !!(rrJobs.length || ["library.rebuild", "library.brief_build"].some(k => activeJobs(k, {asset: x.id}).length));
    if (rrJobs.length) { rr.textContent = rrJobs[0].status === "queued" ? "Queued…" : "Rerolling…"; rr.after(stopButton(rrJobs)); }
    const open = on => { form.hidden = !on; rr.setAttribute("aria-expanded", on); if (on) { field.focus(); rerollSettings(x.id, form); } };
    rr.onclick = () => open(form.hidden);
    $(".reroll-cancel", el).onclick = () => open(false);
    const go = async () => {
      const guidance = field.value.trim();
      try { await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(x.id)}/reroll`, {guidance}); open(false); field.value = ""; await load(); }
      catch (err) { alert(errorText(err)); }
    };
    $(".reroll-go", el).onclick = go;
    field.onkeydown = ev => { if (ev.key === "Enter") { ev.preventDefault(); go(); } else if (ev.key === "Escape") open(false); };
  }

  const cleanJobs = activeJobs("cleanup", {plan: a.plan, asset: x.id});
  const clean = $(".cleanup", el);
  clean.disabled = !a.results.length || !!cleanJobs.length;
  if (!a.results.length) clean.title = "Make 3D first.";
  if (cleanJobs.length) {
    clean.textContent = cleanJobs[0].status === "queued" ? "Queued…" : "Cleaning…";
    clean.after(stopButton(cleanJobs));
  }
  clean.onclick = async () => {
    try { await api(`/api/projects/${slug}/cleanup`, {plan: a.plan, asset: x.id}); await load(); }
    catch (err) { alert(errorText(err)); }
  };

  // fixed layout: the version strip in its own column (reserved even before it loads), the model
  // cards beside it; nothing wraps differently from one poll to the next
  const body = document.createElement("div"); body.className = "ar-body" + (a.plan === "library" ? " with-strip" : "");
  el.append(body);
  if (a.plan === "library") {
    const vs = document.createElement("fieldset"); vs.className = "version-strip"; body.append(vs);
    if (a.results.length || a.current) versionStrip(vs, x.id, () => load());
    else vs.innerHTML = `<legend>Versions</legend><span class="hint">No builds yet.</span>`;
  }
  const models = document.createElement("div"); models.className = "ar-models"; body.append(models);
  if (a.results.length) {
    const r = document.createElement("div"); r.className = "results";
    for (const res of a.results) {
      const c = document.createElement("div"); c.className = "result";
      if (res.current) c.classList.add("current");
      c.dataset.n = res.n ?? "";
      c.innerHTML = `<model-viewer src="${fileUrl(res.glb)}" camera-controls auto-rotate shadow-intensity="0.6"
          alt="3D model of ${esc(a.plan === "library" ? assetName(x.id, x.name) : x.name)}" loading="lazy"></model-viewer>
        <span>${res.n != null ? `<b>v${res.n}</b> ${esc(res.kind === "trellis" ? "TRELLIS" : res.kind)}` : esc(res.name)}${res.seed != null ? ` · seed ${res.seed}` : ""}
          ${res.current ? `<span class="mark ver-used" title="${ACTIVE_TIP}">Active</span>` : ""}</span>
        <span class="hint">${esc(modelStats(res))}</span>
        <span><a href="${fileUrl(res.glb)}" download title="Download this version's 3D model (GLB).">GLB</a>${res.input ? ` · <a href="${fileUrl(res.input)}" target="_blank" title="The image TRELLIS built this model from.">Image sourced</a>` : ""}</span>`;
      r.append(c);
    }
    models.append(r);
  }
  if (a.cleanups.length) {
    const r = document.createElement("div"); r.className = "results";
    for (const c of a.cleanups) {
      const d = c.output_dims_m.map(v => v.toFixed(2)).join(" × ");
      const box = document.createElement("div"); box.className = "result";
      box.innerHTML = `<model-viewer src="${fileUrl(c.glb)}" camera-controls shadow-intensity="0.6"
          alt="Cleaned model of ${esc(a.plan === "library" ? assetName(x.id, x.name) : x.name)}" loading="lazy"></model-viewer>
        <span>Cleaned (${esc(c.usage)}${c.retopo_method ? `, ${esc(c.retopo_method)}` : ""})</span>
        <span class="hint">${(c.output_faces / 1000).toFixed(c.output_faces < 10000 ? 1 : 0)}k faces · ${d} m (plan ${x.dimensions.width} × ${x.dimensions.depth} × ${x.dimensions.height})</span>
        ${c.warnings.length ? `<span class="error">${c.warnings.map(esc).join("; ")}</span>` : ""}
        <span><a href="${fileUrl(c.glb)}" download title="Download the cleaned 3D model (GLB).">GLB</a></span>`;
      r.append(box);
    }
    models.append(r);
  }
  if (!a.results.length && !a.cleanups.length) models.innerHTML = `<span class="hint">No 3D model yet.</span>`;
  return el;
}

$("#cut-views").onclick = async () => {
  try { await api(`/api/projects/${slug}/views/cut`, {}); await load(); }
  catch (err) { alert(errorText(err)); }
};


// Reroll's guidance box: the settings this asset's builder has, in a gray line and the placeholder
async function rerollSettings(asset, form) {
  const line = $(".reroll-settings", form), field = $(".reroll-guidance", form);
  if (line.dataset.loaded === asset) return;
  try {
    const st = await api(`/api/projects/${slug}/library/asset/${encodeURIComponent(asset)}/settings`, undefined, "GET");
    line.textContent = `Settings this builder has: ${st.labels.join(", ")}.`;
    field.placeholder = `Guidance (optional): ${(st.examples || st.labels.slice(0, 4)).join(", ")}…`;
    line.dataset.loaded = asset;
  } catch (err) { line.textContent = ""; }
}
