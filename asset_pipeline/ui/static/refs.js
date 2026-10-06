// Stage 2 (References): reference sheets per plan unit, generated in the background.
const fmtDims = d => `${d.width}×${d.depth}×${d.height} m`;

function renderRefs() {
  const list = $("#refs-list"); list.replaceChildren();
  const busy = data.jobs.some(j => j.kind === "refs.generate" && (j.status === "queued" || j.status === "running"));
  const missing = refs.filter(u => !u.sheets.length).length;
  $("#refs-missing").disabled = busy || !missing;
  $("#refs-missing").textContent = busy ? "Generating…" : `Generate missing (${missing})`;
  const starred = refs.reduce((k, u) => k + u.sheets.filter(s => data.stars[s]).length, 0);
  $("#refs-summary").textContent = refs.length ?
    `${refs.length} assets, ${refs.length - missing} with sheets, ${starred} sheet(s) starred.` :
    "No included assets yet: analyse a scene in the Plan tab.";

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
          <button type="button" class="secondary more" ${busy ? "disabled" : ""}>+ ${+$("#refs-n").value || 2} more</button>
        </div>`;
      const g = document.createElement("div"); g.className = "grid sheets";
      if (!u.sheets.length) g.innerHTML = `<span class="empty">No sheets yet.</span>`;
      u.sheets.forEach(k => g.append(card(k)));
      el.append(g);
      $(".more", el).onclick = () => generateRefs({plan: u.plan, unit: u.key});
      sec.append(el);
    }
    list.append(sec);
  }
}

async function generateRefs(body) {
  try {
    await api(`/api/projects/${slug}/refs/generate`, {...body, n: +$("#refs-n").value || 2});
    await load();
  } catch (err) { alert(err.message); }
}

$("#refs-missing").onclick = () => generateRefs({});
