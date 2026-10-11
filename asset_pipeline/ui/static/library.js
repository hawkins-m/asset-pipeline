// Library › Asset review (world mode phase 3): one card per library entry, with its crop
// from the best current shot frame (object-id pass), the automatic flags (SAM 3.1 check)
// and the user's call: good source / use description / exclude. Decisions are saved per
// entry on the server (site/library_review.json) and survive re-planning.
let libraryData = null;
const lib = {filter: "all"};
try { lib.filter = localStorage.getItem("ap.libFilter") || "all"; } catch (_) { /* ignore */ }
const LIB_DECISIONS = [["source", "Good source"], ["description", "Use description"], ["exclude", "Exclude"]];
const LIB_TIERS = {A: "landmark / kit monument", B: "singular civic", C: "fill typology", D: "prop / vegetation"};

async function loadLibrary() {
  if (!slug) { libraryData = null; return; }
  try { libraryData = await api(`/api/projects/${slug}/library`); } catch (_) { libraryData = null; }
}

function libEntries() {
  const p = libraryData && libraryData.plan;
  return p ? [...p.entries, ...p.unseen.map(u => ({...u, best: null, sources: [], unseen: true}))] : [];
}
const libDecision = e => (libraryData.decisions[e.id] || {}).decision || null;
const LIB_FILTERS = {
  all: ["All assets", () => true], todo: ["Not decided", e => !libDecision(e)],
  flagged: ["Flagged crops", e => !e.unseen && e.flags.some(f => !f.startsWith("approximate"))],
  seen: ["With a crop", e => !e.unseen], unseen: ["Without a crop", e => e.unseen], source: ["Good source", e => libDecision(e) === "source"],
  description: ["Use description", e => libDecision(e) === "description"], exclude: ["Excluded", e => libDecision(e) === "exclude"]};

function libCard(e) {
  const el = document.createElement("div");
  const d = libDecision(e);
  el.className = `asset-card${d ? ` decided-${d}` : ""}`;
  el.dataset.entry = e.id;
  const src = e.best ? e.best.split("/").slice(1).join(" / ").replace(/batch_0*/, "b").replace(/frame_0*(\d)/, "f$1") : "";
  const flags = e.flags.map(f => `<li class="${f.startsWith("approximate") ? "note" : "flag"}">${esc(f)}</li>`).join("");
  el.innerHTML = `${e.best ? `<a href="${fileUrl(e.best)}" target="_blank" title="Open the source frame">
        <img loading="lazy" alt="" src="${fileUrl(`site/library/crops/${e.id}.jpg`)}"></a>`
      : `<div class="no-crop">no current shot frame shows it</div>`}
    <div class="asset-body">
      <div class="asset-name"><b>${esc(e.id)}</b> <span class="chip tier-${e.tier}" title="${esc(LIB_TIERS[e.tier])}">${e.tier}</span></div>
      <div class="hint" style="margin:0">${esc(e.part !== e.typology ? `${e.part} of ${e.typology}` : e.typology)}${e.material ? ` · ${esc(e.material)}` : ""}
        · ${e.instances}×${e.best ? ` · ${(100 * e.share).toFixed(1)}% of ${esc(src)}` : ""}</div>
      ${flags ? `<ul class="asset-flags">${flags}</ul>` : `<div class="asset-ok">no flags</div>`}
      <div class="asset-actions" role="group" aria-label="Decision for ${esc(e.id)}">
        ${LIB_DECISIONS.map(([k, label]) => `<button type="button" data-decision="${k}" aria-pressed="${d === k}"
          ${k === "source" && !e.best ? "disabled title=\"No crop to use\"" : ""}>${label}</button>`).join("")}
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

async function libDecide(id, decision) {
  const cur = (libraryData.decisions[id] || {}).decision || null;
  const next = cur === decision ? null : decision;          // pressing the chosen button again clears it
  try { libraryData.decisions = await api(`/api/projects/${slug}/library/decision`, {entry: id, decision: next}); }
  catch (err) { alert(err.message); return; }
  renderLibrary();
}

$("#assets-list").addEventListener("click", e => {
  const b = e.target.closest("[data-decision]"); if (!b) return;
  libDecide(b.closest("[data-entry]").dataset.entry, b.dataset.decision);
});
$("#lib-filter").onchange = e => { lib.filter = e.target.value; try { localStorage.setItem("ap.libFilter", lib.filter); } catch (_) { /* ignore */ } renderLibrary(); };
$("#lib-plan").onclick = async () => { try { await api(`/api/projects/${slug}/library/plan`, {}); } catch (err) { alert(err.message); } };
$("#lib-check").onclick = async () => { try { await api(`/api/projects/${slug}/library/check`, {}); } catch (err) { alert(err.message); } };
