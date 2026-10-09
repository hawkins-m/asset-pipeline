// World mode frames (per shot, onto the greybox passes) and the moodboard (Style tab).
let draftApplied = Number(sessionStorage.getItem("ap.draftApplied") || 0);

function renderFrames() { keepFocus(renderFramesNow); }
const openArchives = new Set();

// A frame's role: design reference (guides generation, never an asset source) or source.
function roleControl(fr) {
  const w = document.createElement("div"); w.className = "role";
  const tip = "design reference: guides generation (a shot's or a typology's reference), never an asset-library source. " +
    "source: the asset library may derive assets from it (when starred).";
  w.innerHTML = `<select title="${esc(tip)}"><option value="">role…</option>
      <option value="design_ref"${fr.role === "design_ref" ? " selected" : ""}>design ref</option>
      <option value="source"${fr.role === "source" ? " selected" : ""}>source</option></select>
    <input placeholder="note" value="${esc(fr.note || "")}" data-draft="note:${esc(fr.key)}">`;
  const [sel, note] = [$("select", w), $("input", w)];
  const save = async () => {
    try { await api(`/api/projects/${slug}/role`, {path: fr.key, role: sel.value || null, note: note.value}); delete drafts[`note:${fr.key}`]; }
    catch (err) { alert(err.message); }
    await load();
  };
  sel.onchange = save;
  note.onchange = save;
  note.oninput = () => { drafts[`note:${fr.key}`] = note.value; };
  if (drafts[`note:${fr.key}`] !== undefined) note.value = drafts[`note:${fr.key}`];
  return w;
}

function renderFramesNow() {
  const f = framesData, list = $("#frames-list"); list.replaceChildren();
  const s = f.settings;
  $("#frames-settings").textContent = `${s.model === "union" ? "Union Pro 2.0" : "depth LoRA"}: depth ${s.depth_strength}` +
    (s.model === "union" ? ` to ${s.depth_end}, canny ${s.canny_strength} to ${s.canny_end}` : "") + `, ${s.steps} steps`;
  const missing = activeJobs("frames.generate", {missing: true});
  const without = f.shots.filter(r => !r.batches.length).length;
  $("#frames-missing").disabled = !!missing.length || !without;
  $("#frames-missing").textContent = missing.length ? "Generating…" : `Generate for shots without frames (${without})`;
  setStop("#frames-missing-stop", missing);
  if (!f.shots.length) { list.innerHTML = `<p class="hint">No shots yet: build the site and render its passes (Site · Shots).</p>`; return; }
  const approved = Object.keys(data.stars).filter(k => data.stars[k] && k.startsWith("frames/"));
  for (const r of f.shots) {
    const el = document.createElement("section"); el.className = "unit";
    const ok = r.rendered && !r.stale;
    el.innerHTML = `<div class="unit-head"><span class="title">${esc(r.id)}</span><span class="chip">${esc(r.tier)}</span>${r.mood ? `<span class="hint">mood only: no assets</span>` : ""}
        <span class="hint">${ok ? "" : "passes missing or stale: render them in Site · Shots"}</span>
        <label class="hint">ref <select class="ref"><option value="">none</option>${approved.filter(k => !k.startsWith(`frames/${r.id}/`))
          .map(k => `<option>${esc(k)}</option>`).join("")}</select></label>
        <button type="button" class="secondary more" ${ok ? "" : "disabled"}>+ ${+$("#frames-n").value || 4}</button>
        <button type="button" class="regen" title="re-render this shot's passes if the greybox or its camera changed, then generate">Regenerate this shot</button></div>
      ${r.prompt ? `<p class="hint prompt">${esc(r.prompt)}</p>` : ""}`;
    const jobs = activeJobs("frames.generate", {shot: r.id}), cam = activeJobs("shots.camera", {shot: r.id});
    if (jobs.length) {
      for (const b of [$(".more", el), $(".regen", el)]) { b.disabled = true; b.textContent = "Generating…"; }
      $(".regen", el).after(stopButton(jobs));
    }
    if (cam.length) { const b = $(".regen", el); b.disabled = true; b.textContent = "Moving camera…"; }
    $(".more", el).onclick = () => {
      const ref = $(".ref", el).value;
      framesAction({shot: r.id, refs: ref ? [ref] : []});
    };
    $(".regen", el).onclick = async () => {
      const ref = $(".ref", el).value;
      try { await api(`/api/projects/${slug}/shots/${r.id}/regenerate`, {n: +$("#frames-n").value || 4, refs: ref ? [ref] : []}); }
      catch (err) { alert(err.message); }
      await load();
    };
    el.append(shotEditPanel(r));
    const g = document.createElement("div"); g.className = "grid small";
    for (const [label, key] of [["greybox", r.preview], ["depth", r.depth], ["edges", r.canny]]) {
      const c = document.createElement("div"); c.className = "card";
      const u = fileUrl(key, `${fileVersion}-${r.rendered || ""}`);    // the pass's render time
      c.innerHTML = `<a href="${u}" target="_blank"><img loading="lazy" alt="" src="${u}"></a><span class="label">${label}</span>`;
      g.append(c);
    }
    const frameCard = (b, fr) => {
      const c = card(fr.key, {label: `match ${fr.edge_match.toFixed(2)}${b.refs.length ? " · ref" : ""}`});
      c.append(roleControl(fr));
      return c;
    };
    const current = [...r.batches].reverse().filter(b => !b.archived), old = [...r.batches].reverse().filter(b => b.archived);
    for (const b of current) for (const fr of b.frames) g.append(frameCard(b, fr));
    el.append(g);
    if (old.length) {   // batches made on an older greybox: kept, viewable, never asset sources
      const det = document.createElement("details"); det.className = "archived";
      det.open = openArchives.has(r.id);
      det.ontoggle = () => det.open ? openArchives.add(r.id) : openArchives.delete(r.id);
      const n = old.reduce((a, b) => a + b.frames.length, 0), stars = old.flatMap(b => b.frames).filter(f => data.stars[f.key]).length;
      det.innerHTML = `<summary>Older layouts: ${old.length} batch(es), ${n} frames${stars ? `, ${stars} starred` : ""}</summary>`;
      const og = document.createElement("div"); og.className = "grid small";
      for (const b of old) for (const fr of b.frames) og.append(frameCard(b, fr));
      det.append(og); el.append(det);
    }
    list.append(el);
  }
}

async function framesAction(body) {
  try { await api(`/api/projects/${slug}/frames/generate`, {...body, n: +$("#frames-n").value || 4}); }
  catch (err) { alert(err.message); }
  await load();
}
$("#frames-missing").onclick = () => framesAction({});

function renderMoodboard() {
  const el = $("#moodboard-groups"); el.replaceChildren();
  for (const [group, keys] of Object.entries(data.moodboard || {})) {
    const sec = document.createElement("div"); sec.className = "batch";
    sec.innerHTML = `<h3>${esc(group)} (${keys.length})</h3>`;
    const g = document.createElement("div"); g.className = "grid small";
    keys.forEach(k => g.append(card(k, {onSelect: key => { selectedScene = key; render(); }, selected: k === selectedScene})));
    sec.append(g); el.append(sec);
  }
  const drafting = activeJobs("style.draft");
  $("#style-draft").disabled = !Object.keys(data.moodboard || {}).length || !!drafting.length;
  $("#style-draft").textContent = drafting.length ? "Drafting…" : "Draft style text from moodboard";
  setStop("#style-draft-stop", drafting);
  // a finished draft fills the style text box once (it's saved only with the anchor buttons)
  const done = data.jobs.filter(j => j.kind === "style.draft" && j.status === "done" && j.id > draftApplied)
    .sort((a, b) => b.id - a.id)[0];
  if (done) {
    const st = $("#anchor-form [name=style_text]");
    st.value = done.result; st.dataset.touched = "1";
    draftApplied = done.id; sessionStorage.setItem("ap.draftApplied", draftApplied);
  }
}

$("#moodboard-form").onsubmit = async e => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("group", e.target.group.value);
  for (const file of e.target.files.files) fd.append("files", file);
  try { await api(`/api/projects/${slug}/moodboard`, fd); e.target.reset(); }
  catch (err) { alert(err.message); }
  await load();
};
$("#style-draft").onclick = async () => {
  try { await api(`/api/projects/${slug}/style/draft`, {}); } catch (err) { alert(err.message); }
  await load();
};
$("#anchor-text-only").onclick = async () => {
  const st = $("#anchor-form [name=style_text]");
  try { await api(`/api/projects/${slug}/style/text`, {text: st.value}); delete st.dataset.touched; }
  catch (err) { alert(err.message); }
  await load();
};
