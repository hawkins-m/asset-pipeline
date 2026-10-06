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
      <span class="actions"><button type="button" class="make3d">Make 3D</button></span>
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
  return el;
}

$("#cut-views").onclick = async () => {
  try { await api(`/api/projects/${slug}/views/cut`, {}); await load(); }
  catch (err) { alert(err.message); }
};
