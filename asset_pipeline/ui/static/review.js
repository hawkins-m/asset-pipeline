// Stages 3-4 (Review): cut views from starred sheets, choose one per asset, tag
// game / cine / hero, and send it to 3D (TRELLIS.2 on GPU 0).
const USAGES = ["game", "cine", "hero"];

function renderReview() {
  if (!reviewData) return;
  const pending = reviewData.pending_sheets.length;
  const cutJobs = activeJobs("views.cut");
  $("#cut-views").disabled = !pending || !!cutJobs.length;
  $("#cut-views").textContent = cutJobs.length ? "Cutting…" : `Cut views (${pending} new sheet${pending === 1 ? "" : "s"})`;
  setStop("#cut-stop", cutJobs);
  const withViews = reviewData.assets.filter(a => a.views.length).length;
  const built = reviewData.assets.filter(a => a.results.length).length;
  $("#review-summary").textContent = reviewData.assets.length ?
    `${reviewData.starred_sheets} starred sheet(s); ${withViews} of ${reviewData.assets.length} assets have views, ${built} have a 3D model.` :
    "No assets yet: analyse a scene in the Plan tab.";

  const list = $("#review-list"); list.replaceChildren();
  for (const plan of [...new Set(reviewData.assets.map(a => a.plan))]) {
    const sec = document.createElement("section");
    sec.innerHTML = `<h3>${esc(plan)}</h3>`;
    for (const a of reviewData.assets.filter(x => x.plan === plan)) sec.append(assetReview(a));
    list.append(sec);
  }
}

function assetReview(a) {
  const el = document.createElement("div"); el.className = "asset-review";
  const x = a.asset, d = x.dimensions;
  el.innerHTML = `<div class="head">
      <span class="title">${esc(x.name)}</span>
      <span class="hint">×${x.count} · ${d.width}×${d.depth}×${d.height} m${x.kit ? ` · kit ${esc(x.kit)}` : ""}</span>
      <span class="usage" role="group" aria-label="Usage">${USAGES.map(u =>
        `<button type="button" data-u="${u}" aria-pressed="${x.usage === u}">${u}</button>`).join("")}</span>
      <span class="actions"><button type="button" class="make3d">Make 3D</button>
        <button type="button" class="secondary cleanup" title="Scale to the plan size, pivot at the base${x.usage === "game" ? ", decimate and bake (game)" : ""}">Clean up</button></span>
    </div>`;
  el.querySelectorAll(".usage button").forEach(b => b.onclick = async () => {
    try { await api(`/api/projects/${slug}/review/usage`, {plan: a.plan, asset: x.id, usage: b.dataset.u}); await load(); }
    catch (err) { alert(err.message); }
  });

  const g = document.createElement("div"); g.className = "grid views";
  if (!a.views.length) g.innerHTML = `<span class="hint">No views yet: star a sheet in References, then <em>Cut views</em>.</span>`;
  for (const v of a.views) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "view" + (v.key === a.chosen ? " chosen" : "");
    b.setAttribute("aria-pressed", v.key === a.chosen);
    b.title = `${v.sheet}, view ${v.position}${v.asked ? ` (asked: ${v.asked})` : ""}`;
    b.innerHTML = `<img loading="lazy" alt="" src="${fileUrl(v.key)}">
      <span class="label">${v.key === a.chosen ? "✓ " : ""}${esc(v.sheet.split("/").pop().replace(".png", "").replace("sheet_", "s"))} v${v.position}</span>`;
    b.onclick = async () => {
      try { await api(`/api/projects/${slug}/review/choose`, {plan: a.plan, asset: x.id, view: v.key === a.chosen ? null : v.key}); await load(); }
      catch (err) { alert(err.message); }
    };
    g.append(b);
  }
  el.append(g);

  const jobs3d = activeJobs("3d.trellis", {plan: a.plan, asset: x.id});
  const make = $(".make3d", el);
  make.disabled = !a.chosen || !!jobs3d.length;
  make.title = a.chosen ? "" : "Choose a view first";
  if (jobs3d.length) {
    make.textContent = jobs3d[0].status === "queued" ? "Queued…" : "Building…";
    make.after(stopButton(jobs3d));
  }
  make.onclick = async () => {
    try { await api(`/api/projects/${slug}/3d`, {plan: a.plan, asset: x.id, mode: $("#trellis-mode").value}); await load(); }
    catch (err) { alert(err.message); }
  };

  const cleanJobs = activeJobs("cleanup", {plan: a.plan, asset: x.id});
  const clean = $(".cleanup", el);
  clean.disabled = !a.results.length || !!cleanJobs.length;
  if (!a.results.length) clean.title = "Make 3D first";
  if (cleanJobs.length) {
    clean.textContent = cleanJobs[0].status === "queued" ? "Queued…" : "Cleaning…";
    clean.after(stopButton(cleanJobs));
  }
  clean.onclick = async () => {
    try { await api(`/api/projects/${slug}/cleanup`, {plan: a.plan, asset: x.id}); await load(); }
    catch (err) { alert(err.message); }
  };

  if (a.results.length) {
    const r = document.createElement("div"); r.className = "results";
    for (const res of a.results) {
      const c = document.createElement("div"); c.className = "result";
      c.innerHTML = `<model-viewer src="${fileUrl(res.glb)}" camera-controls auto-rotate shadow-intensity="0.6"
          alt="3D model of ${esc(x.name)}" loading="lazy"></model-viewer>
        <span>${esc(res.name)}</span>
        <span class="hint">${res.faces ? `${(res.faces / 1000).toFixed(0)}k faces` : ""}${res.raw_ratio ? ` · raw F/V ${res.raw_ratio}` : ""}${res.peak_vram_gb ? ` · ${res.peak_vram_gb} GB` : ""}</span>
        <span><a href="${fileUrl(res.glb)}" download>GLB</a> · <a href="${fileUrl(res.input)}" target="_blank">what TRELLIS saw</a></span>`;
      r.append(c);
    }
    el.append(r);
  }
  if (a.cleanups.length) {
    const r = document.createElement("div"); r.className = "results";
    for (const c of a.cleanups) {
      const d = c.output_dims_m.map(v => v.toFixed(2)).join(" × ");
      const box = document.createElement("div"); box.className = "result";
      box.innerHTML = `<model-viewer src="${fileUrl(c.glb)}" camera-controls shadow-intensity="0.6"
          alt="Cleaned model of ${esc(x.name)}" loading="lazy"></model-viewer>
        <span>Cleaned (${esc(c.usage)}${c.retopo_method ? `, ${esc(c.retopo_method)}` : ""})</span>
        <span class="hint">${(c.output_faces / 1000).toFixed(c.output_faces < 10000 ? 1 : 0)}k faces · ${d} m (plan ${x.dimensions.width} × ${x.dimensions.depth} × ${x.dimensions.height})</span>
        ${c.warnings.length ? `<span class="error">${c.warnings.map(esc).join("; ")}</span>` : ""}
        <span><a href="${fileUrl(c.glb)}" download>GLB</a></span>`;
      r.append(box);
    }
    el.append(r);
  }
  return el;
}

$("#cut-views").onclick = async () => {
  try { await api(`/api/projects/${slug}/views/cut`, {}); await load(); }
  catch (err) { alert(err.message); }
};
