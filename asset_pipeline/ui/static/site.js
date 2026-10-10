// World mode (Site and Shots tabs): greybox build state, aerial previews, per-shot control passes.
const SITE_JOBS = ["site.build", "site.extract", "site.preview", "shots.render"];

function renderSite() {
  const s = siteData;
  const busy = SITE_JOBS.flatMap(k => activeJobs(k));
  const sum = s.summary;
  $("#site-init").hidden = s.layout;
  $("#site-build").disabled = !s.layout || !!busy.length;
  $("#site-build").textContent = sum ? "Rebuild greybox" : "Build greybox";
  for (const id of ["#site-extract", "#site-preview", "#shots-render"]) $(id).disabled = !sum || !!busy.length;
  setStop("#site-stop", busy);
  $("#site-summary").innerHTML = !s.layout ? "No site layout yet (world mode not started)." :
    !sum ? "Layout ready; build the greybox." :
    `${esc(s.blend)} · <strong>${sum.edited ? "edited in Blender" : "as built"}</strong>` +
    (sum.stale ? " · <span class=\"error\">changed since last read: Re-read .blend</span>" : "") +
    `<br>${sum.slots} slots: ${Object.entries(sum.types).map(([t, n]) => `${n} ${esc(t)}`).join(", ")}` +
    (sum.untagged.length ? `<br><span class="error">${sum.untagged.length} untagged meshes</span>` : "");

  const pv = $("#site-previews"); pv.replaceChildren();
  s.previews.forEach(k => { const el = document.createElement("div"); el.className = "card";
    el.innerHTML = `<a href="${fileUrl(k)}" target="_blank"><img loading="lazy" alt="" src="${fileUrl(k)}"></a>
      <span class="label">${esc(k.split("/").slice(-2, -1)[0])}</span>`; pv.append(el); });

  const list = $("#shots-list"); list.replaceChildren();
  for (const r of s.shots) {
    const el = document.createElement("section"); el.className = "unit";
    const state = !r.rendered ? "not rendered" : r.stale ? "stale (greybox changed)" : `rendered ${r.rendered.replace("T", " ")}`;
    el.innerHTML = `<div class="unit-head"><span class="title">${esc(r.id)}</span><span class="chip">${esc(r.tier)}</span>
      <span class="hint">${r.lens_mm} mm · ${r.resolution.join("×")}${r.district ? ` · ${esc(r.district)}` : ""} · ${state}</span>
      <button type="button" class="secondary render">Render passes</button></div>
      ${r.warnings.map(w => `<p class="error">${esc(w)}</p>`).join("")}`;
    const shotJobs = activeJobs("shots.render", {shot: r.id});
    const b = $(".render", el);
    b.disabled = !!busy.length;
    if (shotJobs.length) { b.textContent = "Rendering…"; b.after(stopButton(shotJobs)); }
    b.onclick = () => siteAction("shots/render", {shots: [r.id]});
    const g = document.createElement("div"); g.className = "grid small";
    for (const [name, key] of Object.entries(r.passes)) {
      const c = document.createElement("div"); c.className = "card";
      const u = fileUrl(key, `${fileVersion}-${r.rendered || ""}`);
      c.innerHTML = `<a href="${u}" target="_blank"><img loading="lazy" alt="" src="${u}"></a><span class="label">${name}</span>`;
      g.append(c);
    }
    if (!Object.keys(r.passes).length) g.innerHTML = `<span class="empty">No passes yet.</span>`;
    el.append(g); list.append(el);
  }
}

async function siteAction(path, body = {}) {
  try { await api(`/api/projects/${slug}/${path}`, body); }
  catch (err) {
    if (err.message.includes("edited in Blender") && confirm(`${err.message}. Rebuild anyway? (the old file is kept as greybox.prev.blend)`))
      return siteAction(path, {...body, force: true});
    if (!err.message.includes("edited in Blender")) alert(err.message);
  }
  await load();
}

$("#site-init").onclick = () => siteAction("site/init");
$("#site-build").onclick = () => siteAction("site/build");
$("#site-extract").onclick = () => siteAction("site/extract");
$("#site-preview").onclick = () => siteAction("site/preview");
$("#shots-render").onclick = () => siteAction("shots/render", {shots: null});
