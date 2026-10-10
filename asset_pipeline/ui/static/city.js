// City tab: the catalog as a table plus a grouped detail form, one section at a time
// (building types, districts, materials, zones, rules) and the plan report. Edits stay in a
// per-section draft until Save (the 2 s job polling never touches it); Save writes the whole
// section through the existing catalog API, which validates it.
const TYPE_COLUMNS = ["none", "accent", "order"];
const DISTRICT_COLUMNS = ["none", "rare", "accent", "accent_on_civic_only"];
// field: [key, label, kind, options]; kinds as in edit.js plus share (0-1 slider, empty =
// no cap), material (a material id or none) and ref (an image path: design references,
// moodboard images or anything typed)
const CITY = {
  typologies: {title: "Building types", tips: "typologies", find: "name or family",
    cols: [["id", "Type"], ["family", "Family"], ["_share", "Share", "num"]],
    groups: [
      ["Massing", true, [["form", "Form", "select", FORMS], ["storeys", "Storeys", "pair"], ["width", "Width (m)", "pair"],
        ["depth", "Depth (m)", "pair"], ["family", "Family", "select", FAMILIES], ["columns", "Columns", "select", TYPE_COLUMNS]]],
      ["Placement", false, [["place", "Places", "list"], ["site", "Placed on", "select", SITES], ["ground", "Ground floor", "list"],
        ["merge_chance", "Merge chance", "num"]]],
      ["Look", false, [["prompt", "In frames", "area"], ["desc", "Asset sheet", "area"], ["roofs", "Roofs", "list"],
        ["facades", "Façades", "list"], ["material", "Material", "material"]]],
      ["Limits", true, [["max_share", "Max share", "share"], ["max_count", "Max count", "optnum"], ["ref", "Landmark ref", "ref", "wide"],
        ["ref_strength", "Ref strength", "num"]]]]},
  districts: {title: "Districts", tips: "districts", find: "district",
    cols: [["id", "District"], ["columns", "Columns"], ["height_bias", "Height", "num"]],
    groups: [
      ["Identity", false, [["id", "Id", "text"], ["notes", "Identity", "area"]]],
      ["Typology mix", false, [["mix.core", "Core", "map"], ["mix.middle", "Middle", "map"], ["mix.edge", "Edge", "map"]]],
      ["Look", true, [["palette", "Palette", "map", "wide"], ["columns", "Columns", "select", DISTRICT_COLUMNS],
        ["height_bias", "Height bias", "num"], ["merge_chance", "Merge chance (x)", "num"]]]]},
  materials: {title: "Materials", tips: "materials", find: "material",
    cols: [["id", "Material"], ["districts", "Districts", "list"], ["ref", "Ref", "flag"]],
    groups: [
      ["Words", false, [["id", "Id", "text"], ["words", "In frames", "area"]]],
      ["Where", false, [["types", "Slot types", "list"], ["districts", "Only districts", "list"]]],
      ["Reference", true, [["ref", "Image", "ref", "wide"], ["ref_strength", "Strength", "num"]]]]},
  overlays: {title: "Zones", tips: "overlays", find: "zone",
    cols: [["id", "Zone"], ["adds", "Adds", "map"]],
    groups: [["Zone", false, [["id", "Zone", "text"], ["adds", "Adds (type: weight)", "map"],
      ["replaces_housing_with", "Hillside: housing becomes", "opttext"]]]]},
  rules: {title: "Rules", find: "rule", cols: [["id", "Rules"]], fixed: true},
  report: {title: "Plan report", cols: [], fixed: true},
};
const TIP_KEY = {"replaces_housing_with": "rep", "id": "id"};
let citySection = "typologies", cityFind = "";
const cityDrafts = {};   // section -> {rows, base (server JSON), sel, dirty: Set(row index:key), errors: {}}
const citySel = {};      // section -> selected row index

// --- section data <-> rows ------------------------------------------------------------------
function sectionRows(name) {
  const c = catalogData;
  if (name === "overlays") return Object.entries(c.overlays || {}).map(([z, o]) => ({id: z, ...o}));
  if (name === "rules") return [{id: "Repetition checks", _key: "checks", ...c.checks}, {id: "Variation", _key: "variation", ...c.variation}];
  if (name === "report") return [];
  return structuredClone(c[name] || []);
}
function draftFor(name) {
  const base = JSON.stringify(sectionRows(name));
  let d = cityDrafts[name];
  if (!d || (!d.dirty.size && d.base !== base)) {      // no unsaved edits: follow the server
    d = cityDrafts[name] = {rows: JSON.parse(base), base, dirty: new Set(), errors: {}, added: false};
  }
  return d;
}
const isDirty = name => !!(cityDrafts[name] && cityDrafts[name].dirty.size);

// --- rendering ------------------------------------------------------------------------------
function renderCatalog() {
  const wrap = $("#catalog");
  wrap.hidden = !catalogData || !catalogData.city;
  if (wrap.hidden) return;
  const sec = $("#city-section");
  if (!sec.options.length) {
    sec.innerHTML = Object.entries(CITY).map(([k, s]) => `<option value="${k}">${esc(s.title)}</option>`).join("");
    sec.value = citySection;
  }
  [...sec.options].forEach(o => { o.textContent = CITY[o.value].title + (isDirty(o.value) ? " *" : ""); });
  $("#city-find").placeholder = CITY[citySection].find || "";
  $("#city-find").disabled = citySection === "report";
  renderCityTable();
  // the detail form is rebuilt only when what it shows changed: never under the caret
  const d = citySection === "report" ? null : draftFor(citySection);
  const key = citySection === "report" ? `report|${JSON.stringify(catalogData.report)}|${catalogData.plan_mtime}|${activeJobs("site.plan").length}`
    : `${citySection}|${citySel[citySection]}|${d.base}|${refsKey()}`;
  if (key !== detailShown || !$("#city-detail").children.length) {
    if (d && d.dirty.size && detailShown && detailShown.startsWith(`${citySection}|${citySel[citySection]}|`)) return;  // keep edits
    detailShown = key; renderCityDetail();
  }
}
let detailShown = "";
const refsKey = () => (framesData ? framesData.shots : []).flatMap(r => r.batches.flatMap(b => b.frames.filter(f => f.role === "design_ref").map(f => f.key))).join(",");

function cellText(row, [key, , kind]) {
  if (key === "_share") {
    const sh = ((catalogData.report || {}).repetition || {}).share || {};
    return sh[row.id] != null ? `${Math.round(sh[row.id] * 100)}%` : "";
  }
  const v = getPath(row, key);
  return kind === "list" ? toList(v) : kind === "map" ? toMap(v) : kind === "flag" ? (v ? "yes" : "") : v ?? "";
}

function visibleRows(name) {
  const d = draftFor(name), q = cityFind.trim().toLowerCase();
  return d.rows.map((r, i) => [r, i]).filter(([r]) => !q || [r.id, r.family, r.form].some(x => x && String(x).toLowerCase().includes(q)));
}

function renderCityTable() {
  const S = CITY[citySection], t = $("#city-table");
  const left = $("#city-left");
  left.hidden = citySection === "report";
  if (left.hidden) return;
  const d = draftFor(citySection);
  if (citySel[citySection] == null || citySel[citySection] >= d.rows.length) citySel[citySection] = d.rows.length ? 0 : null;
  const rows = visibleRows(citySection);
  // order building types by footprint share, as in the plan report; others keep file order
  if (citySection === "typologies") {
    const sh = ((catalogData.report || {}).repetition || {}).share || {};
    rows.sort((a, b) => (sh[b[0].id] || 0) - (sh[a[0].id] || 0));
  }
  t.innerHTML = `<thead><tr>${S.cols.map(c => `<th class="${c[2] === "num" ? "num" : ""}">${esc(c[1])}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(([r, i]) => `<tr data-i="${i}" aria-selected="${i === citySel[citySection]}">${S.cols.map((c, ci) =>
      `<td class="${c[2] === "num" ? "num" : ""}${ci === 0 && [...d.dirty].some(k => k.startsWith(`${i}:`)) ? " dirty" : ""}">${esc(cellText(r, c) || (ci === 0 ? "(new)" : ""))}</td>`).join("")}</tr>`).join("")}</tbody>`;
  t.onclick = e => { const tr = e.target.closest("tr[data-i]"); if (tr) selectCityRow(+tr.dataset.i); };
  $("#city-add").hidden = $("#city-dup").hidden = $("#city-remove").hidden = !!S.fixed;
  $("#city-dup").disabled = $("#city-remove").disabled = citySel[citySection] == null;
}

function selectCityRow(i) {
  citySel[citySection] = i;
  detailShown = "";
  renderCityTable(); renderCatalog();
  const tr = $(`#city-table tr[data-i="${i}"]`); if (tr) tr.scrollIntoView({block: "nearest"});
}

function refOptions(current) {
  // design references (frames), then moodboard images; the current value stays even if neither
  const out = [];
  for (const r of (framesData ? framesData.shots : [])) for (const b of r.batches) b.frames.forEach((f, k) => {
    if (f.role === "design_ref") out.push([f.key, `${r.id} · ${b.dir.split("/").pop().replace("batch_", "batch ")} · frame ${k + 1} (design reference)`]);
  });
  for (const [g, keys] of Object.entries(data.moodboard || {})) keys.forEach(k => out.push([k, `moodboard ${g} · ${k.split("/").pop()}`]));
  if (current && !out.some(([k]) => k === current)) out.unshift([current, current]);
  return [["", "(none)"], ...out];
}

function fieldControl(name, row, i, [key, label, kind, opts], mine) {
  const v = getPath(row, key), id = `cf-${key.replace(".", "-")}`;
  const err = (cityDrafts[name].errors || {})[`${i}:${key}`];
  const attrs = `id="${id}" data-k="${key}" data-kind="${kind}"${err ? ` aria-invalid="true" title="${esc(err)}"` : ""}`;
  if (kind === "area") return `<textarea rows="2" ${attrs}>${esc(v ?? "")}</textarea>`;
  if (kind === "select") return `<select ${attrs}>${opts.map(o => `<option${o === v ? " selected" : ""}>${o}</option>`).join("")}</select>`;
  if (kind === "material") return `<select ${attrs}><option value="">(district palette)</option>${(catalogData.materials || [])
    .map(m => `<option${m.id === v ? " selected" : ""}>${esc(m.id)}</option>`).join("")}</select>`;
  if (kind === "ref") return `<select ${attrs}>${refOptions(v).map(([k, l]) => `<option value="${esc(k)}"${k === (v || "") ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  if (kind === "share") {
    const on = v != null;
    return `<span class="slider"><input type="range" min="0" max="100" step="1" ${attrs} value="${on ? Math.round(v * 100) : 20}" ${on ? "" : "disabled"}>
      <output class="val">${on ? `${Math.round(v * 100)}%` : ""}</output>
      <label class="check"><input type="checkbox" data-nocap ${on ? "" : "checked"}>no cap</label></span>`;
  }
  const text = kind === "list" ? toList(v) : kind === "map" ? toMap(v) : kind === "pair" ? (v ? v.join(" – ") : "") : (v ?? "");
  const num = kind === "num" || kind === "optnum";
  return `<input ${attrs} class="${["pair", "num", "optnum", "map"].includes(kind) ? "val" : ""}"${num ? ` type="number" step="any"` : ""}
    ${kind === "optnum" ? `placeholder="none"` : ""} value="${esc(text)}">`;
}

function renderCityDetail() {
  const box = $("#city-detail"), S = CITY[citySection];
  if (citySection === "report") { box.innerHTML = reportHtml(); bindReport(box); return; }
  const d = draftFor(citySection), i = citySel[citySection];
  if (i == null) { box.innerHTML = `<p class="hint">Nothing selected.</p>`; return; }
  const row = d.rows[i];
  const mine = new Set(row.user_fields || []);
  const tips = TIPS[S.tips || (row._key === "checks" ? "checks" : "variation")] || {};
  let groups = S.groups;
  if (citySection === "rules") {      // one group of numbers per rules object
    groups = [[row.id, true, Object.keys(row).filter(k => !["id", "_key"].includes(k)).map(k => [k, k.replaceAll("_", " "), "num"])]];
  }
  const sectionMine = (catalogData.section_edits || []).includes(citySection === "rules" ? row._key : citySection);
  box.innerHTML = `<div class="detail-head"><b>${esc(row.id || "(new)")}</b>
      ${mine.size ? `<span class="mine-tag" title="${esc([...mine].join(", "))}">edited by you: ${esc([...mine].join(", "))}</span>`
        : sectionMine ? `<span class="mine-tag">edited by you</span>` : `<span class="auto-tag">auto</span>`}</div>
    ${groups.map(([g, two, fields]) => `<fieldset><legend>${esc(g)}</legend><div class="form${two ? " two" : ""}">
      ${fields.map(f => {
        const m = mine.has(f[0].split(".")[0]);
        const tip = (tips[TIP_KEY[f[0]] || f[0]] || "") + (m ? "\n(edited by you)" : "");
        const wide = two && (f[3] === "wide" || f[2] === "area");
        return `<label for="cf-${f[0].replace(".", "-")}" title="${esc(tip)}" class="${m ? "mine" : ""}">${esc(f[1])}:</label>
          <div class="${wide ? "span" : ""}" title="${esc(tip)}">${fieldControl(citySection, row, i, f, m)}</div>`;
      }).join("")}</div></fieldset>`).join("")}
    <div class="row end"><span class="hint" id="city-dirty" style="margin:0 auto 0 0"></span>
      <button type="button" id="city-revert">Revert</button><button type="button" id="city-save" class="default">Save</button></div>`;
  box.oninput = e => onCityInput(e.target, i);
  box.onchange = e => onCityInput(e.target, i);
  $("#city-revert").onclick = cityRevert;
  $("#city-save").onclick = citySave;
  updateCityDirty();
}

function onCityInput(el, i) {
  const d = cityDrafts[citySection], row = d.rows[i];
  if (el.matches("[data-nocap]")) {
    const r = el.closest(".slider").querySelector("input[type=range]");
    r.disabled = el.checked;
    el = r;
  }
  const key = el.dataset.k, kind = el.dataset.kind;
  if (!key) return;
  const s = el.value;
  let v;
  try {
    v = kind === "list" ? fromList(s) : kind === "map" ? fromMap(s) : kind === "pair" ? fromPair(s.replace(/[–-]/g, ","))
      : kind === "num" ? (s.trim() === "" || isNaN(+s) ? (() => { throw new Error("expected a number"); })() : +s)
      : kind === "optnum" ? optNum(s) : kind === "opttext" || kind === "material" || kind === "ref" ? optText(s)
      : kind === "share" ? (el.disabled ? null : +s / 100) : s;
    delete d.errors[`${i}:${key}`]; el.removeAttribute("aria-invalid");
  } catch (err) {
    d.errors[`${i}:${key}`] = err.message; el.setAttribute("aria-invalid", "true"); el.title = err.message;
    d.dirty.add(`${i}:${key}`); updateCityDirty(); return;
  }
  if (kind === "share") { const o = el.closest(".slider").querySelector("output"); o.textContent = v == null ? "" : `${Math.round(v * 100)}%`; }
  setPath(row, key, v);
  const orig = JSON.parse(d.base)[i];
  if (orig && JSON.stringify(getPath(orig, key)) === JSON.stringify(v)) d.dirty.delete(`${i}:${key}`); else d.dirty.add(`${i}:${key}`);
  if (key === "id" || key === "family") renderCityTable();
  updateCityDirty();
}

function dirtyFieldNames() {
  const d = cityDrafts[citySection];
  if (!d) return [];
  return [...new Set([...d.dirty].map(k => k.split(":")[1]).filter(k => k !== "_row").map(k => k.replace("mix.", "mix ")))];
}
function updateCityDirty() {
  const d = cityDrafts[citySection], names = dirtyFieldNames();
  const el = $("#city-dirty");
  if (el) el.textContent = Object.keys(d.errors).length ? "Fix the marked fields before saving." : d.dirty.size ? "Unsaved changes" : "";
  const save = $("#city-save"), rev = $("#city-revert");
  if (save) save.disabled = !d.dirty.size || !!Object.keys(d.errors).length;
  if (rev) rev.disabled = !d.dirty.size;
  [...$("#city-section").options].forEach(o => { o.textContent = CITY[o.value].title + (isDirty(o.value) ? " *" : ""); });
  $("#city-table").querySelectorAll("tr[data-i]").forEach(tr => {
    tr.firstElementChild.classList.toggle("dirty", [...d.dirty].some(k => k.startsWith(`${tr.dataset.i}:`)));
  });
  if (currentTab === "city") $("#sb-left").textContent = statusLeft.city() || TAB_NAMES.city;
}
statusLeft.city = () => {
  const d = cityDrafts[citySection];
  return d && d.dirty.size ? `Unsaved: ${dirtyFieldNames().join(", ") || "rows"}` : "";
};

function cityRevert() {
  delete cityDrafts[citySection]; detailShown = "";
  renderCatalog();
}

async function citySave() {
  const d = cityDrafts[citySection];
  if (Object.keys(d.errors).length) return;
  let path = citySection, body;
  if (citySection === "overlays") {
    body = Object.fromEntries(d.rows.filter(r => r.id).map(r => [r.id, {adds: r.adds || {}, replaces_housing_with: r.replaces_housing_with || null}]));
  } else if (citySection === "rules") {
    try {
      for (const r of d.rows) {
        if (![...d.dirty].some(k => k.startsWith(`${d.rows.indexOf(r)}:`))) continue;
        const {id, _key, ...vals} = r;   // eslint-disable-line no-unused-vars
        await api(`/api/projects/${slug}/catalog/${_key}`, vals, "PUT");
      }
      delete cityDrafts.rules; noteCatalogSaved();
    } catch (err) { alert(err.message); return; }
    await load(); return;
  } else {
    body = d.rows.map(r => { const x = {...r}; delete x._share; return x; });
  }
  try { await api(`/api/projects/${slug}/catalog/${path}`, body, "PUT"); }
  catch (err) { alert(err.message); return; }
  delete cityDrafts[citySection]; noteCatalogSaved();
  await load();
}

function cityAddRow(copy) {
  const d = draftFor(citySection), i = citySel[citySection];
  const row = copy && i != null ? {...structuredClone(d.rows[i]), id: `${d.rows[i].id}_copy`, user_fields: []} : {id: ""};
  d.rows.push(row);
  d.dirty.add(`${d.rows.length - 1}:_row`);
  cityFind = ""; $("#city-find").value = "";
  selectCityRow(d.rows.length - 1);
  const f = $("#city-detail [data-k=id]"); if (f) { f.focus(); f.select(); }
}
function cityRemoveRow() {
  const d = draftFor(citySection), i = citySel[citySection];
  if (i == null || !confirm(`Remove ${d.rows[i].id || "this row"}? (applies when you save)`)) return;
  d.rows.splice(i, 1);
  // indices after i shift: rebuild the dirty set against the remaining rows
  const moved = new Set();
  for (const k of d.dirty) { const [n, f] = k.split(":"); if (+n < i) moved.add(k); else if (+n > i) moved.add(`${+n - 1}:${f}`); }
  moved.add("-1:_removed");
  d.dirty = moved; d.errors = {};
  citySel[citySection] = Math.min(i, d.rows.length - 1);
  detailShown = "";
  renderCatalog();
}

// --- plan report ------------------------------------------------------------------------------
function reportHtml() {
  const r = catalogData.report || {}, rep = r.repetition;
  const planJobs = activeJobs("site.plan");
  return `<fieldset><legend>Plan</legend><div class="form">
      <span class="lbl">Buildings:</span><span class="val">${r.buildings ? r.buildings.toLocaleString("en") : "not planned yet"}</span>
      ${rep ? `<span class="lbl">Largest shares:</span><span>${Object.entries(rep.share).slice(0, 8).map(([k, v]) => `${esc(k)} ${(v * 100).toFixed(0)}%`).join(", ")}</span>
      <span class="lbl">Diversity:</span><span>${Object.entries(rep.diversity).map(([k, v]) => `${esc(k)} <span class="val">${v}</span>`).join(", ")}</span>
      <span class="lbl">Columns:</span><span class="val">${(rep.column_share * 100).toFixed(1)}% of buildings</span>
      <span class="lbl">Longest run:</span><span class="val">${rep.longest_identical_run}</span>
      <span class="lbl">Thin tiles:</span><span class="val">${rep.thin_tiles} / ${rep.urban_tiles}</span>` : ""}
    </div></fieldset>
    <fieldset><legend>Warnings</legend>${(r.warnings || []).map(w => `<p class="error">${esc(w)}</p>`).join("") || `<p>No warnings.</p>`}</fieldset>
    <div class="row"><button type="button" id="city-replan" class="default">${planJobs.length ? "Planning…" : "Re-plan (F5)"}</button>
      ${catalogData.plan_image ? `<a href="${fileUrl(catalogData.plan_image, catalogData.plan_mtime)}" target="_blank">Open plan image</a>` : ""}</div>
    ${catalogData.plan_image ? `<div class="viewport"><img alt="City plan" src="${fileUrl(catalogData.plan_image, catalogData.plan_mtime)}"></div>` : ""}`;
}
function bindReport(box) {
  const b = $("#city-replan", box);
  b.disabled = !!activeJobs("site.plan").length;
  b.onclick = () => siteAction("site/plan");
}

// --- wiring ---------------------------------------------------------------------------------
$("#city-section").onchange = e => { citySection = e.target.value; cityFind = ""; $("#city-find").value = ""; detailShown = ""; renderCatalog(); updateCityStatus(); };
$("#city-find").oninput = e => { cityFind = e.target.value; renderCityTable(); };
$("#city-add").onclick = () => cityAddRow(false);
$("#city-dup").onclick = () => cityAddRow(true);
$("#city-remove").onclick = cityRemoveRow;
$("#city-table").addEventListener("keydown", e => {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  e.preventDefault();
  const ids = visibleRows(citySection).map(([, i]) => i);
  const k = ids.indexOf(citySel[citySection]);
  const n = ids[Math.max(0, Math.min(ids.length - 1, k + (e.key === "ArrowDown" ? 1 : -1)))];
  if (n != null) selectCityRow(n);
});
function updateCityStatus() { if (cityDrafts[citySection]) updateCityDirty(); }
saveHandlers.city = () => { if (citySection !== "report" && isDirty(citySection)) citySave(); };
revertHandlers.city = () => { if (isDirty(citySection)) cityRevert(); };
window.addEventListener("beforeunload", e => { if (Object.keys(CITY).some(isDirty)) { e.preventDefault(); e.returnValue = ""; } });
