// Library › Asset review (world mode phase 3): one card per library entry, with its crop
// from the best current shot frame (object-id pass), the automatic flags (SAM 3.1 check)
// and the user's call: good source / use description / exclude. Decisions are saved per
// entry on the server (site/library_review.json) and survive re-planning.
let libraryData = null;
const lib = {filter: "all"};
try { lib.filter = localStorage.getItem("ap.libFilter") || "all"; } catch (_) { /* ignore */ }
const LIB_DECISIONS = [["source", "Good source"], ["description", "Use description"],
  ["reference", "Description + reference frame"], ["exclude", "Exclude"], ["kit", "Facade kit"]];
const LIB_TIERS = {A: "landmark / kit monument", B: "singular civic", C: "fill typology", D: "prop / vegetation"};

async function loadLibrary() {
  if (!slug) { libraryData = null; return; }
  try { libraryData = await api(`/api/projects/${slug}/library`); } catch (_) { libraryData = null; }
  if (typeof loadAssetReviews === "function") await loadAssetReviews();
}

function libEntries() {
  const p = libraryData && libraryData.plan;
  return p ? [...p.entries, ...p.unseen.map(u => ({...u, best: null, sources: [], unseen: true}))] : [];
}
const LIB_ROUTES = {procedural: "procedural", trellis: "TRELLIS"};
const libRoute = e => (libraryData.decisions[e.id] || {}).route || e.route_auto || null;
const libDecision = e => (libraryData.decisions[e.id] || {}).decision || null;
const LIB_FILTERS = {
  all: ["All assets", () => true], todo: ["Not decided", e => !libDecision(e)],
  flagged: ["Flagged crops", e => !e.unseen && e.flags.some(f => !f.startsWith("approximate"))],
  seen: ["With a crop", e => !e.unseen], unseen: ["Without a crop", e => e.unseen], source: ["Good source", e => libDecision(e) === "source"],
  description: ["Use description", e => libDecision(e) === "description"],
  reference: ["Description + reference frame", e => libDecision(e) === "reference"],
  exclude: ["Excluded", e => libDecision(e) === "exclude"]};
const shortFrame = k => k.split("/").slice(1).join(" / ").replace(/batch_0*/, "b").replace(/frame_0*(\d)/, "f$1").replace(/\.png$/, "");

function libCard(e) {
  const el = document.createElement("div");
  const d = libDecision(e);
  el.className = `asset-card${d ? ` decided-${d}` : ""}`;
  el.dataset.entry = e.id;
  const src = e.best ? shortFrame(e.best) : "";
  const ref = d === "reference" ? libraryData.decisions[e.id].frame : null;
  const foliage = e.kind === "vegetation";
  const flags = e.flags.map(f => `<li class="${f.startsWith("approximate") ? "note" : "flag"}">${esc(f)}</li>`).join("");
  el.innerHTML = `${e.best ? `<a href="${fileUrl(e.best)}" target="_blank" title="Open the source frame">
        <img loading="lazy" alt="" src="${fileUrl(`site/library/crops/${e.id}.jpg`)}"></a>`
      : `<div class="no-crop">no current shot frame shows it</div>`}
    <div class="asset-body">
      <div class="asset-name"><b>${esc((libraryData.decisions[e.id] || {}).name || e.name || e.id)}</b>${((libraryData.decisions[e.id] || {}).name || e.name) ? ` <span class="hint" style="margin:0">${esc(e.id)}</span>` : ""} <span class="chip tier-${e.tier}" title="${esc(LIB_TIERS[e.tier])}">${e.tier}</span>
        ${libRoute(e) ? `<button type="button" class="chip route route-${libRoute(e)}" data-route="${libRoute(e)}"
          title="3D route${(libraryData.decisions[e.id] || {}).route ? " (yours)" : " (default)"}: click to switch">${LIB_ROUTES[libRoute(e)]}</button>` : ""}</div>
      <div class="hint" style="margin:0">${esc(e.part !== e.typology ? `${e.part} of ${e.typology}` : e.typology)}${e.material ? ` · ${esc(e.material)}` : ""}
        · ${e.instances}×${e.best ? ` · ${(100 * e.share).toFixed(1)}% of ${esc(src)}` : ""}</div>
      ${flags ? `<ul class="asset-flags">${flags}</ul>` : `<div class="asset-ok">no flags</div>`}
      ${ref ? `<a class="asset-ref" href="${fileUrl(ref)}" target="_blank" title="Look reference for its sheets">
        <img alt="" src="${fileUrl(ref)}"><span>reference: ${esc(shortFrame(ref))}</span></a>` : ""}
      ${typeof lrAssetsOf === "function" && lrAssetsOf(e.id).length ? `<div class="asset-review">${lrAssetsOf(e.id).map(a => {
          const st = ((lr.list && lr.list.assets.find(x => x.asset === a)) || {}).status;
          return `<button type="button" class="small" data-review="${esc(a)}" title="Build review: reference vs build, brief, rebuild">Build review${lrAssetsOf(e.id).length > 1 ? `: ${esc(a.slice(e.id.length + 1))}` : ""}</button>${st ? ` <span class="mark ${LR_STATUS_CLASS[st]}">${esc(lr.list.statuses[st])}</span>` : ""}`;
        }).join(" ")}</div>` : ""}
      <div class="asset-manage"><button type="button" class="small" data-manage="${esc(e.id)}"
        title="Name, description, route and decision; duplicate or rename">Manage entry…</button>${e.custom ? ` <span class="chip" title="Made in Library › Entries: a re-plan keeps it">yours</span>` : ""}</div>
      <div class="asset-actions" role="group" aria-label="Decision for ${esc(e.id)}">
        ${LIB_DECISIONS.filter(([k]) => k !== "kit" || d === "kit").map(([k, label]) => `<button type="button" data-decision="${k}" aria-pressed="${d === k}"
          title="${d === k && (libraryData.decisions[e.id] || {}).auto ? "Set automatically: no usable crop" : ""}"
          ${foliage ? "disabled title=\"Unreal foliage: not generated\"" : k === "source" && !e.best ? "disabled title=\"No crop to use\"" : ""}>${label}${d === k && (libraryData.decisions[e.id] || {}).auto ? " (auto)" : ""}</button>`).join("")}
      </div>
    </div>`;
  return el;
}

function renderLibrary() {
  const box = $("#assets-list"); if (!box) return;
  const head = $("#assets-summary");
  const planJobs = activeJobs("library.plan"), checkJobs = activeJobs("library.check");
  $("#lib-plan").disabled = !!planJobs.length || !(libraryData && libraryData.has_greybox);
  $("#lib-check").disabled = !!checkJobs.length || !(libraryData && libraryData.plan);
  $("#lib-check").textContent = checkJobs.length ? "Checking…" : "Check crops (SAM)";
  if (!libraryData || !libraryData.plan) {
    head.textContent = libraryData && libraryData.has_greybox ? "No library plan yet: Re-plan builds it from the locked shot frames." :
      "World mode only: build the greybox and lock shot frames first.";
    box.replaceChildren(); return;
  }
  const all = libEntries();
  const nd = all.filter(e => libDecision(e)).length;
  const p = libraryData.plan, out = (p.candidates || []).filter(c => !c.ok);
  head.innerHTML = `${all.length} assets · ${nd} decided · ${all.filter(LIB_FILTERS.flagged[1]).length} flagged ·
    sources: ${Object.values(p.sources).flat().length} current shot frame(s)${out.length ? `, ${out.length} excluded
    <span class="hint" title="${esc(out.map(c => `${c.frame}: ${c.reason}`).join("\n"))}">(old layout or off the greybox: hover)</span>` : ""}
    ${p.checked ? "" : " · <b>not checked yet</b>: run Check crops for the trees / sky flags"}`;
  const f = $("#lib-filter");
  f.innerHTML = Object.entries(LIB_FILTERS).map(([k, [label, fn]]) =>
    `<option value="${k}"${k === lib.filter ? " selected" : ""}>${label} (${all.filter(fn).length})</option>`).join("");
  const shown = all.filter((LIB_FILTERS[lib.filter] || LIB_FILTERS.all)[1]);
  // one grid per district: assets with a crop first, then by tier and typology
  const byD = {};
  for (const e of shown) (byD[e.district || "-"] ||= []).push(e);
  box.replaceChildren();
  for (const d of Object.keys(byD).sort()) {
    const sec = document.createElement("section"); sec.className = "asset-district";
    const es = byD[d].sort((a, b) => (!!a.unseen - !!b.unseen) || a.tier.localeCompare(b.tier) ||
      a.typology.localeCompare(b.typology) || (b.share || 0) - (a.share || 0));
    sec.innerHTML = `<h3>${esc(d)} <span class="hint" style="margin:0">${es.length} · ${es.filter(e => !e.unseen).length} with a crop</span></h3>`;
    const g = document.createElement("div"); g.className = "asset-grid";
    es.forEach(e => g.append(libCard(e)));
    sec.append(g); box.append(sec);
  }
  if (!shown.length) box.innerHTML = `<p class="hint">No assets match.</p>`;
}

async function libDecide(id, decision, frame = null) {
  const cur = (libraryData.decisions[id] || {}).decision || null;
  const next = cur === decision && !frame ? null : decision;          // pressing the chosen button again clears it
  try { libraryData.decisions = await api(`/api/projects/${slug}/library/decision`, {entry: id, decision: next, frame}); }
  catch (err) { alert(errorText(err)); return; }
  renderLibrary();
}

// any frame of any shot (old layouts too) as an asset's look reference
function pickReference(e) {
  const shots = (framesData && framesData.shots || []).filter(r => r.batches.some(b => b.frames.length));
  if (!shots.length) { alert("No frames in this project."); return; }
  const cur = (libraryData.decisions[e.id] || {}).frame || null;
  let shot = cur ? cur.split("/")[1] : (shots.find(r => r.id === `med-${e.district}`) || shots[0]).id, pick = cur;
  const body = document.createElement("div"); body.className = "ref-picker";
  const draw = () => {
    const r = shots.find(x => x.id === shot);
    body.innerHTML = `<label>Shot: <select>${shots.map(x => `<option${x.id === shot ? " selected" : ""}>${esc(x.id)}</option>`).join("")}</select></label>
      <p class="hint" style="margin:4px 0">The frame's look guides the sheets of <b>${esc(e.id)}</b>; its layout doesn't matter, so older-layout frames work too.</p>
      <div class="ref-batches">${[...r.batches].reverse().map(b => `<div class="ref-batch"><div class="hint" style="margin:0">${esc(b.dir.split("/").pop())}${b.archived ? " (older layout)" : ""}</div>
        <div class="ref-thumbs">${b.frames.map(f => `<button type="button" class="thumb${f.key === pick ? " sel" : ""}" data-key="${esc(f.key)}" aria-pressed="${f.key === pick}">
          <img loading="lazy" alt="" src="${fileUrl(f.key)}"><span>${esc(f.key.split("/").pop().replace(".png", ""))}${data.stars[f.key] ? " ★" : ""}${r.locked === f.key ? " · locked" : ""}</span></button>`).join("")}</div></div>`).join("")}</div>`;
    $("select", body).onchange = ev => { shot = ev.target.value; draw(); };
    body.querySelectorAll("[data-key]").forEach(t => t.onclick = () => { pick = t.dataset.key; draw(); });
  };
  draw();
  dialog({title: `Reference frame for ${e.id}`, body, className: "wide", buttons: [
    {label: "Use this frame", default: true, onClick: async () => { if (!pick) { alert("Pick a frame first."); return false; }
      await libDecide(e.id, "reference", pick); }},
    {label: "Cancel"}]});
}

$("#assets-list").addEventListener("click", async e => {
  const mb = e.target.closest("[data-manage]");
  if (mb) { manageEntry(mb.dataset.manage); return; }
  const rb = e.target.closest("[data-review]");
  if (rb) { openAssetReview(rb.dataset.review); return; }
  const r = e.target.closest("[data-route]");
  if (r) {
    const id = r.closest("[data-entry]").dataset.entry, ent = libEntries().find(x => x.id === id);
    const next = r.dataset.route === "procedural" ? "trellis" : "procedural";
    try { libraryData.decisions = await api(`/api/projects/${slug}/library/route`, {entry: id, route: next === ent.route_auto ? null : next}); }
    catch (err) { alert(errorText(err)); return; }
    renderLibrary(); return;
  }
  const b = e.target.closest("[data-decision]"); if (!b) return;
  const id = b.closest("[data-entry]").dataset.entry;
  if (b.dataset.decision === "reference" && libDecision({id}) !== "reference") {
    pickReference(libEntries().find(x => x.id === id)); return;
  }
  libDecide(id, b.dataset.decision);
});
$("#lib-filter").onchange = e => { lib.filter = e.target.value; try { localStorage.setItem("ap.libFilter", lib.filter); } catch (_) { /* ignore */ } renderLibrary(); };
$("#lib-plan").onclick = async () => { try { await api(`/api/projects/${slug}/library/plan`, {}); } catch (err) { alert(errorText(err)); } };
$("#lib-check").onclick = async () => { try { await api(`/api/projects/${slug}/library/check`, {}); } catch (err) { alert(errorText(err)); } };

// --- Entries management: create, duplicate, rename, describe, route, decide ------------------
const entryField = (id, label, input) => `<label for="${id}">${label}</label>${input}`;

async function entriesReload() { await load(); }

// an in-page yes/no (native confirm() can't be driven or seen by the page tests)
function askDialog(title, text, ok = "OK") {
  return new Promise(res => {
    let answered = false;
    const body = document.createElement("p"); body.textContent = text;
    dialog({title, body, onClose: () => { if (!answered) res(false); }, buttons: [
      {label: ok, default: true, onClick: () => { answered = true; res(true); }}, {label: "Cancel"}]});
  });
}

// run an entries call; "files already exist" asks before overwriting, then retries
async function entriesCall(path, body) {
  try { return await api(`/api/projects/${slug}/library/entries${path}`, body); }
  catch (err) {
    const msg = errorText(err);
    if (/^files already exist/.test(msg) && !body.overwrite && await askDialog("Overwrite?", `${msg}\n\nOverwrite them?`, "Overwrite"))
      return entriesCall(path, {...body, overwrite: true});
    alert(msg); return null;
  }
}

function newEntryDialog() {
  const f = document.createElement("form"); f.className = "form"; f.id = "entry-new-form";
  f.innerHTML = `${entryField("en-id", "Id:", `<input id="en-id" name="id" placeholder="stone-counter" required pattern="[a-z0-9](?:[a-z0-9]|-)*">`)}
    ${entryField("en-name", "Name:", `<input id="en-name" name="name" placeholder="Stone counter">`)}
    ${entryField("en-desc", "Description:", `<textarea id="en-desc" name="description" rows="3" placeholder="What the sheets should show"></textarea>`)}
    ${entryField("en-kind", "Kind:", `<select id="en-kind" name="kind"><option value="prop">prop</option><option value="building">building</option></select>`)}
    ${entryField("en-route", "3D route:", `<select id="en-route" name="route"><option value="">default for the kind (props TRELLIS, buildings procedural)</option>
      <option value="procedural">procedural</option><option value="trellis">TRELLIS</option></select>`)}
    ${entryField("en-size", "Size (m):", `<input id="en-size" name="size" placeholder="w d h, e.g. 3 1 1 (optional)">`)}
    ${entryField("en-tier", "Tier:", `<select id="en-tier" name="tier"><option value="">default (prop D, building B)</option>${Object.keys(LIB_TIERS).map(t => `<option>${t}</option>`).join("")}</select>`)}`;
  dialog({title: "New library entry", body: f, buttons: [
    {label: "Create", default: true, onClick: async () => {
      if (!f.reportValidity()) return false;
      const fd = Object.fromEntries(new FormData(f));
      const size = (fd.size || "").split(/[\s,x×]+/).filter(Boolean).map(Number);
      if (size.some(x => !(x > 0)) || ![0, 1, 3].includes(size.length)) { alert("Size: one number or width depth height, in metres."); return false; }
      const r = await entriesCall("", {id: fd.id, name: fd.name || null, description: fd.description || null, kind: fd.kind,
        route: fd.route || null, tier: fd.tier || null, size_m: size.length ? (size.length === 1 ? size[0] : size) : null});
      if (!r) return false;
      await entriesReload();
    }},
    {label: "Cancel"}]});
}

function idPrompt({title, intro, label, value, button, onGo}) {
  const f = document.createElement("form"); f.className = "form"; f.id = "entry-id-form";
  f.innerHTML = `<p class="hint" style="grid-column:1/-1">${intro}</p>${entryField("en-newid", label, `<input id="en-newid" value="${esc(value)}" required pattern="[a-z0-9](?:[a-z0-9]|-)*">`)}
    ${entryField("en-newname", "Name:", `<input id="en-newname" placeholder="optional">`)}`;
  return dialog({title, body: f, buttons: [
    {label: button, default: true, onClick: async () => {
      if (!f.reportValidity()) return false;
      return (await onGo($("#en-newid", f).value.trim(), $("#en-newname", f).value.trim())) ? undefined : false;
    }},
    {label: "Cancel"}]});
}

async function manageEntry(id) {
  const ent = libEntries().find(x => x.id === id); if (!ent) return;
  const dec = libraryData.decisions[id] || {};
  const custom = !!ent.custom;
  const body = document.createElement("form"); body.className = "form"; body.id = "entry-form";
  const nm = dec.name || ent.name || "";
  body.innerHTML = `<p class="hint" style="grid-column:1/-1">Entry <b>${esc(id)}</b>${custom ? " (yours)" : " (from the library plan: its id is fixed; rename changes the display name only, duplicate to get an id of your own)"}.</p>
    ${entryField("en-name", "Name:", `<input id="en-name" value="${esc(nm)}" placeholder="${esc(id.replace(/-/g, " "))}">`)}
    ${entryField("en-desc", "Description:", `<textarea id="en-desc" rows="4" placeholder="${esc(ent.description_auto || "")}">${esc(dec.description || "")}</textarea>
      <span></span><span class="hint" style="margin:0">${dec.description ? "Yours" : "Automatic"}: <button type="button" id="en-desc-reset" class="small"${dec.description ? "" : " disabled"}>Reset to auto</button></span>`)}
    ${entryField("en-route", "3D route:", `<select id="en-route"><option value="">auto (${LIB_ROUTES[ent.route_auto] || "none"})</option>
      <option value="procedural"${dec.route === "procedural" ? " selected" : ""}>procedural</option><option value="trellis"${dec.route === "trellis" ? " selected" : ""}>TRELLIS</option></select>`)}
    ${entryField("en-decision", "Decision:", `<select id="en-decision"><option value="">not decided</option>${LIB_DECISIONS.filter(([k]) => k !== "kit" || dec.decision === "kit")
      .map(([k, l]) => `<option value="${k}"${dec.decision === k ? " selected" : ""}${k === "source" && !ent.best ? " disabled" : ""}>${l}</option>`).join("")}</select>`)}`;
  const reload = async () => { await entriesReload(); };
  $("#en-desc-reset", body).onclick = async () => {
    try { await api(`/api/projects/${slug}/library/entries/${id}/description`, {description: null}); } catch (err) { alert(errorText(err)); return; }
    $("#en-desc", body).value = ""; await reload();
  };
  const d = dialog({title: `Entry: ${id}`, body, className: "wide", buttons: [
    {label: "Save", default: true, onClick: async () => {
      try {
        const desc = $("#en-desc", body).value.trim(), name = $("#en-name", body).value.trim();
        if (desc !== (dec.description || "")) await api(`/api/projects/${slug}/library/entries/${id}/description`, {description: desc || null});
        if (name !== nm && name) await api(`/api/projects/${slug}/library/entries/${id}/rename`, {name});
        const route = $("#en-route", body).value || null;
        if (route !== (dec.route || null)) await api(`/api/projects/${slug}/library/route`, {entry: id, route});
        const decision = $("#en-decision", body).value || null;
        if (decision !== (dec.decision || null)) {
          if (decision === "reference") { alert("Pick the reference frame with the card's 'Description + reference frame' button."); }
          else await api(`/api/projects/${slug}/library/decision`, {entry: id, decision});
        }
      } catch (err) { alert(errorText(err)); return false; }
      await reload();
    }},
    {label: "Duplicate…", onClick: () => { setTimeout(() => idPrompt({title: `Duplicate ${id}`, label: "New id:", value: `${id}-copy`, button: "Duplicate",
      intro: "Copies the entry with its description, route, decision, sheets, views, brief, build and TRELLIS results. The copy starts unreviewed; the original is untouched.",
      onGo: async (to, name) => { const r = await entriesCall(`/${id}/duplicate`, {to, name: name || null}); if (r) await reload(); return !!r; }}), 0); }},
    {label: "Rename…", onClick: () => { setTimeout(() => {
      if (custom) idPrompt({title: `Rename ${id}`, label: "New id:", value: id, button: "Rename",
        intro: "Moves the entry's sheets, views, brief, build and results to the new id (files are renamed, not copied) and keeps its review. Anything that refers to the old id elsewhere (areas, Unreal) must be updated by hand.",
        onGo: async (to, name) => {
          if (to !== id && !(await askDialog("Rename entry", `Rename ${id} to ${to}? Its files move to the new id.`, "Rename"))) return false;
          const r = await entriesCall(`/${id}/rename`, {to, name: name || null}); if (r) await reload(); return !!r; }});
      else {
        const f = document.createElement("form"); f.className = "form";
        f.innerHTML = `<p class="hint" style="grid-column:1/-1">${esc(id)} comes from the library plan, so its id stays; only the display name changes.</p>
          ${entryField("en-newname", "Name:", `<input id="en-newname" value="${esc(nm)}" required>`)}`;
        dialog({title: `Rename ${id}`, body: f, buttons: [{label: "Rename", default: true, onClick: async () => {
          if (!f.reportValidity()) return false;
          const r = await entriesCall(`/${id}/rename`, {name: $("#en-newname", f).value.trim()}); if (!r) return false; await reload(); }},
          {label: "Cancel"}]});
      }
    }, 0); }},
    {label: "Close"}]});
}
$("#lib-new").onclick = newEntryDialog;
