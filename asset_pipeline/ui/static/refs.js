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
