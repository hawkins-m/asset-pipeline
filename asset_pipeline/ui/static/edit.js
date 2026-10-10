// World mode editing helpers: value <-> text conversions and field tips shared with the City
// tab (city.js), and per-shot prompt / camera edits in the Frames tab. Everything is saved
// to the project's site/layout.json. Fields the user changed are marked "yours"; the rest
// is auto (generated or imported).
let catalogData = null;
const drafts = {};                  // unsaved per-shot inputs, by data-draft key
const openEdits = new Set();        // shots whose edit panel is open

// --- value <-> text -------------------------------------------------------------------------
const toList = v => (v || []).join(", ");
const fromList = s => s.split(",").map(x => x.trim()).filter(Boolean);
const toMap = v => Object.entries(v || {}).map(([k, x]) => `${k}: ${x}`).join(", ");
function fromMap(s) {
  const out = {};
  for (const part of s.split(",").map(x => x.trim()).filter(Boolean)) {
    const [k, v] = part.split(":").map(x => x.trim());
    if (!k || v === undefined || isNaN(+v)) throw new Error(`"${part}": expected name: number`);
    out[k] = +v;
  }
  return out;
}
const toPair = v => v ? v.join(", ") : "";
function fromPair(s) {
  if (!s.trim()) return null;
  const v = s.split(/[,\s]+/).filter(Boolean).map(Number);
  if (v.length !== 2 || v.some(isNaN)) throw new Error(`"${s}": expected two numbers`);
  return v;
}
const optNum = s => s.trim() === "" ? null : +s;
const optText = s => s.trim() || null;

// field: [key, label, kind, options]; kind: text | area | num | optnum | list | map | pair | select | opttext
const FAMILIES = ["civic", "housing", "mixed", "infrastructure", "public_realm"];
const FORMS = ["perimeter", "courtyard", "bar", "l_shape", "u_shape", "stepped", "tower", "podium_tower", "crescent",
  "row", "hall", "drum", "cavea", "cube", "arcade_line", "pavilion", "open"];
const SITES = ["auto", "block", "corridor", "shore", "pier_end", "node_gate", "kit"];
// Tooltips: what each field does, its unit, and whether it applies city-wide or per row.
// Keep these in step with city_types.py / s0_frames.py when behaviour changes.
const TIPS = {
  districts: {
    id: "District id (per district). The city's blocks take the district of the nearest civic node, so this must match a node id to be used.",
    notes: "Identity line (per district). Added once to the prompt of a shot whose focus district this is (else the district covering most of the frame). Keep it to one line.",
    "mix.core": "Typology weights for this district's dense blocks (density >= 0.6). Format type: weight. Relative weights, no unit. A type not listed here never appears in these blocks, unless a zone adds it.",
    "mix.middle": "Typology weights for this district's middle blocks (density 0.4 to 0.6). Format type: weight. Relative weights, no unit.",
    "mix.edge": "Typology weights for this district's outer blocks (density < 0.4). Format type: weight. Relative weights, no unit.",
    palette: "Material weights for this district's buildings. Format material: weight; relative weights, no unit. One material is picked per typology per tile (tile_m, 400 m). Typologies with a pinned material ignore this.",
    columns: "Column policy (per district). none: no columns. rare: about 10% of accent-type buildings. accent: about 30%, and always when the facade is a colonnade or pilasters. accent_on_civic_only: accents on civic buildings only. Typologies with columns = order always get them.",
    height_bias: "Storeys added to every building in this district (per district). Can be negative. Unit: storeys of 3.4 m (city storey_m).",
    merge_chance: "Multiplies each typology's merge chance in this district (per district). 1 = as the typology says, 0 = never merge here, 2 = twice as often (a chance can't pass 1).",
  },
  materials: {
    id: "Material id (per material). District palettes and pinned typology materials refer to it.",
    words: "Exact words a prompt uses for this material. A prompt names at most 2 materials, each covering at least 3% of the frame; a visible landmark's material always comes first.",
    types: "Greybox slot types this material covers when a slot has no material tag: kit monuments, paving, water... City buildings are tagged from the district palette, so this list doesn't affect them.",
    districts: "Only these districts (per material). Limits the slot-type match above, and the prompts: the material is named only in shots whose focus district is one of these, so wide shots never name it (keep an accent material of one quarter off the whole city). Empty = every district.",
    ref: "Optional reference image (project-relative path). Applied as Redux masked to this material's pixels only (from the shot's id pass).",
    ref_strength: "Redux strength of the reference (per material), 0 to 2, default 0.15. Higher carries more of the image's look, and more of its content.",
  },
  typologies: {
    id: "Typology id (city-wide catalog). Used by district mixes and zones, and as the slot type and asset id.",
    family: "Family. housing and mixed count as housing in the footprint share. civic matters for the accent_on_civic_only column policy. housing/mixed perimeter or row types fill the rest of a block around a single building. public_realm slots are structures, not buildings.",
    form: "Greybox massing, which is what the frames follow. Fill forms (perimeter, row, bar, stepped, crescent, courtyard up to 30 m wide) fill their block. Every other form is one building, and the rest of the block gets housing.",
    site: "Where it's placed. auto: inferred from form and place. block: assigned to city blocks. corridor: across a green valley or along the promenade. shore: steps into the sea. pier_end: the harbour pier head. node_gate: where avenues leave a civic core. kit: a kit monument placed by the civic cores.",
    width: "Footprint width range in metres (min, max). Per house for row. Length of each bar for bar. Arc length for crescent. Perimeter blocks take their frontage from the city's parcel size instead.",
    depth: "Footprint depth range in metres (min, max): the wing depth for perimeter and courtyard forms.",
    storeys: "Storey range (min, max); one storey is 3.4 m (city storey_m). Each building also gets the city's jitter, its district's height bias, and +avenue bonus on an avenue.",
    place: "Zones it may go in: core, avenue, interior, crossing, waterfront, hillside, corridor, edge, node_ring. Empty = anywhere. Every block counts as interior.",
    prompt: "Words a frame prompt uses when this type is on screen: up to 3 types by coverage. A landmark (one with a reference image) is always named, first.",
    desc: "Asset description (city-wide). It seeds the phase 3 library asset's description, which drives its reference sheets. Frame prompts don't use it.",
    roofs: "Roof variants, picked per building. Modelled shapes: roof_garden, garden_crown, planted_walk, pergola, solar_pergola, terracotta_hip/pitch, pitched_bronze, barrel_vault, solar_vault, vault_series, sawtooth, shallow_dome, half_domes, bronze_crown, lantern, skylight. Any other name is a flat roof.",
    facades: "Facade variants, picked per building, never the same three in a row. A name containing 'arcade' recesses the ground floor 3.5 m. loggia_top sets the top storey back. Names containing colonnade or pilasters count as columns. Other names are labels only.",
    ground: "Ground-floor uses, picked per building. shops, cafe and market are preferred on avenue frontage. Labels only: they don't change the massing.",
    columns: "Columns. none: never. accent: per the district's column policy. order: always, as for full colonnades and temples.",
    max_share: "Cap per district: the share (0 to 1) of a district's built footprint this type may reach (the plan report measures it the same way). Checked once the district has more than 20 buildings. Empty = no cap.",
    merge_chance: "Chance (0 to 1, per typology) that a block given this type merges with 1-3 neighbouring blocks of the same district into one large building, up to 1.5x / 2x / 2.5x its size range. The streets between them go. Single-building forms only (hall, cube, courtyard, drum...), not fill forms. Multiplied by the district's merge chance.",
    max_count: "City-wide cap on placements: blocks or line features, not individual buildings. Empty = no cap.",
    material: "Pinned material id (city-wide for this type). Overrides the district palette. For a landmark, its words are named first in prompts.",
    ref: "Landmark reference image (project-relative path). Applied as Redux masked to this type's slots only, and makes the type a landmark: always named first in prompts.",
    ref_strength: "Redux strength of the landmark reference, 0 to 2, default 0.12. Keep it low: it's 'inspired by', not a copy.",
  },
  overlays: {
    zone: "Zone name (city-wide). waterfront: within 250 m of the shore. hillside: ground slope over 0.12. corridor: within 80 m of a green corridor. crossing: within 90 m of an avenue crossing. node_ring: within 80 m outside a civic node.",
    adds: "Typology weights added to every district's mix wherever this zone applies (city-wide). Format type: weight; same relative scale as the district mixes.",
    rep: "Hillside only: every housing type in the mix is replaced by this typology on steep ground (its weights move to it).",
  },
  checks: {
    max_typology_share_city: "City-wide: the largest share (0 to 1) of the built footprint one typology may cover. The generator starts backing off at 85% of it; a report warning above it.",
    max_typology_share_district: "The largest share (0 to 1) of a district's built footprint one typology may cover. Same threshold for every district, checked per district with 20+ buildings.",
    min_types_per_urban_tile: "Fewest distinct typologies a tile (tile_m, 400 m) with 25+ buildings should show. Warns when more than 20% of such tiles fall short.",
    max_identical_run: "Most neighbours in a row on one frontage with the same typology, facade, roof and storeys. City-wide.",
    min_height_cv_block: "Least height spread within a block (std / mean of building heights; blocks of 3+ buildings). Warns when more than 25% of blocks fall short. City-wide.",
    max_column_share: "City-wide: the largest share (0 to 1) of buildings with columns.",
    min_walkable: "Hill towns (organic nodes): the least share (0 to 1) of buildings within 1.6x the node's radius that stand within 20 m of a street, lane or stair. The report also shows the city-wide share, for information (ordinary blocks have inner courts and second rows).",
    district_diversity_min: "Least typology diversity per district: the Shannon index of footprint shares, in nats. e^value is about the number of equally used types (1.6 = about 5). Checked per district with 20+ buildings.",
  },
  variation: {
    storeys_jitter: "City-wide: random storeys added or removed per building (plus or minus this many).",
    avenue_bonus: "City-wide: storeys added to perimeter and row buildings whose frontage is on an avenue.",
    step_back_top: "City-wide: chance (0 to 1) that a building of 4+ storeys and over 9 m deep sets its top storey back. Always for loggia_top facades.",
    height_noise: "City-wide strength of the height field, in storeys: buildings at the field's highs gain up to this many, at its lows lose as many. The field is smooth and drifts across each district (its own pattern per district), so heights vary by place rather than in rings around the nodes. 0 = off.",
    height_noise_scale_m: "City-wide scale of the height field, in metres: roughly the distance between a high and the next low. Smaller = choppier, larger = broad rises and dips across a district.",
    accent_chance: "City-wide chance (0 to 1) that a housing or mixed-use building of 3+ storeys within 90 m of an avenue crossing rises as an accent.",
    accent_storeys: "City-wide: storeys an accent building rises above its neighbours.",
    corner_bonus: "City-wide: perimeter-block corners (the first and last building of each street edge with 2+ buildings) rise 1 to this many storeys above the rest. It marks the corners and adds height spread within a block. 0 = off.",
  },
};
const tipAttr = (section, key, mine) => {
  const t = (TIPS[section] || {})[key] || "";
  return ` title="${esc(t + (mine ? "\n(edited by you)" : ""))}"`;
};

const getPath = (o, k) => k.split(".").reduce((x, p) => (x || {})[p], o);
function setPath(o, k, v) {
  const ps = k.split("."); let x = o;
  ps.slice(0, -1).forEach(p => { x[p] = x[p] || {}; x = x[p]; });
  x[ps.at(-1)] = v;
}

// --- per-shot edits (Frames tab) -------------------------------------------------------------

function shotEditPanel(r) {
  const pp = r.prompt_parts || {}, e = r.edit || {};
  const d = k => drafts[`${r.id}:${k}`];
  const val = (k, server) => d(k) ?? server;
  const source = {auto: "auto", append: "auto + yours", override: "yours (override)"}[pp.source] || "";
  const el = document.createElement("details");
  el.className = "shot-edit"; el.open = openEdits.has(r.id);
  el.ontoggle = () => el.open ? openEdits.add(r.id) : openEdits.delete(r.id);
  const n = (k, i) => val(`${k}${i}`, (e[k] || [0, 0, 0])[i]);
  el.innerHTML = `<summary>Prompt and camera <span class="${pp.source === "auto" ? "auto-tag" : "mine-tag"}">prompt: ${source}</span>
      <span class="${e.camera_edited ? "mine-tag" : "auto-tag"}">camera: ${e.camera_edited ? "yours" : "auto"}</span></summary>
    <div class="col">
      <label>Auto prompt (built from the camera's view, the catalog and the materials)<p class="auto-prompt">${esc(pp.auto || "")}</p></label>
      <label class="${pp.append ? "mine" : ""}">Append (yours)<textarea rows="2" data-draft="${r.id}:append">${esc(val("append", pp.append || ""))}</textarea></label>
      <label class="${pp.override ? "mine" : ""}">Override (yours: replaces the auto prompt)<textarea rows="2" data-draft="${r.id}:override">${esc(val("override", pp.override || ""))}</textarea></label>
      <div class="row"><button type="button" class="save-prompt">Save prompt</button>
        <button type="button" class="secondary reset-prompt">Reset to auto</button></div>
      ${e.in_layout ? `<label class="${(e.refs || []).length ? "mine" : ""}" title="Frames (project-relative paths, one per line) added to this shot's style references (Redux) every time it's generated. Use them for design references: frames whose layout or look this shot should keep. Strength is the total added; keep it low (0.05): more carries the reference's sky and can replace buildings.">Design references (yours)<textarea rows="2" data-draft="${r.id}:refs">${esc(val("refs", (e.refs || []).join("\n")))}</textarea></label>
      <div class="row"><label title="Total Redux strength added for these references, 0 to 1. Default 0.05.">ref strength<input type="number" step="0.01" data-draft="${r.id}:ref_strength" value="${esc(val("ref_strength", e.ref_strength ?? 0.05))}"></label>
        <button type="button" class="save-refs">Save references</button></div>` : ""}
    </div>
    ${e.in_layout ? `<div class="row camera">
      <label class="${e.lens_override ? "mine" : ""}">lens mm (auto ${e.lens_auto})<input type="number" step="any" data-draft="${r.id}:lens" placeholder="auto" value="${esc(val("lens", e.lens_override ?? ""))}"></label>
      <label class="${e.tier_override ? "mine" : ""}">shot type<select data-draft="${r.id}:tier">${["", "wide", "medium", "tight"].map(t =>
        `<option value="${t}"${t === val("tier", e.tier_override || "") ? " selected" : ""}>${t || `auto (${e.tier_auto})`}</option>`).join("")}</select></label>
      ${["right", "up", "forward"].map((a, i) => `<label class="${(e.nudge_pos || [])[i] ? "mine" : ""}">move ${a} m<input type="number" step="any" data-draft="${r.id}:nudge_pos${i}" value="${n("nudge_pos", i)}"></label>`).join("")}
      ${["right", "up", "forward"].map((a, i) => `<label class="${(e.nudge_target || [])[i] ? "mine" : ""}">aim ${a} m<input type="number" step="any" data-draft="${r.id}:nudge_target${i}" value="${n("nudge_target", i)}"></label>`).join("")}
      <button type="button" class="save-camera">Save camera</button><button type="button" class="secondary reset-camera">Reset camera</button>
    </div><p class="hint">Nudges are metres in the camera's own frame, on top of the auto position. Saving writes the camera into the .blend; this shot's passes then re-render on Regenerate.</p>`
      : `<p class="hint">This camera isn't in the layout yet (added in Blender); saving a prompt adopts it.</p>`}`;
  el.addEventListener("input", ev => { const k = ev.target.dataset.draft; if (k) drafts[k] = ev.target.value; });
  const clear = prefix => Object.keys(drafts).filter(k => k.startsWith(`${r.id}:${prefix}`)).forEach(k => delete drafts[k]);
  const savePrompt = async (append, override) => {
    try { await api(`/api/projects/${slug}/shots/${r.id}/prompt`, {append, override}, "PUT"); clear("append"); clear("override"); }
    catch (err) { alert(err.message); }
    await load();
  };
  $(".save-prompt", el).onclick = () => savePrompt($("[data-draft$=':append']", el).value, $("[data-draft$=':override']", el).value);
  $(".reset-prompt", el).onclick = () => savePrompt("", null);
  if (e.in_layout) $(".save-refs", el).onclick = async () => {
    const refs = $(`[data-draft="${r.id}:refs"]`, el).value.split("\n").map(s => s.trim()).filter(Boolean);
    try { await api(`/api/projects/${slug}/shots/${r.id}/refs`, {refs, ref_strength: +$(`[data-draft="${r.id}:ref_strength"]`, el).value}, "PUT");
      clear("refs"); clear("ref_strength"); }
    catch (err) { alert(err.message); }
    await load();
  };
  const saveCam = async body => {
    try { await api(`/api/projects/${slug}/shots/${r.id}/camera`, body, "PUT");
      ["lens", "tier", "nudge_pos", "nudge_target"].forEach(clear); }
    catch (err) { alert(err.message); }
    await load();
  };
  if (e.in_layout) {
    const num = k => +($(`[data-draft="${r.id}:${k}"]`, el).value || 0);
    $(".save-camera", el).onclick = () => saveCam({
      lens_override: optNum($(`[data-draft="${r.id}:lens"]`, el).value), tier_override: $(`[data-draft="${r.id}:tier"]`, el).value || null,
      nudge_pos: [0, 1, 2].map(i => num(`nudge_pos${i}`)), nudge_target: [0, 1, 2].map(i => num(`nudge_target${i}`))});
    $(".reset-camera", el).onclick = () => saveCam({lens_override: null, tier_override: null, nudge_pos: [0, 0, 0], nudge_target: [0, 0, 0]});
  }
  return el;
}

// Re-rendering replaces the DOM every poll: put the caret back where it was.
function keepFocus(fn) {
  const a = document.activeElement, key = a && a.dataset && a.dataset.draft;
  const sel = key && "selectionStart" in a ? [a.selectionStart, a.selectionEnd] : null;
  fn();
  if (!key) return;
  const b = document.querySelector(`[data-draft="${CSS.escape(key)}"]`);
  if (b) { b.focus(); if (sel && b.setSelectionRange) try { b.setSelectionRange(...sel); } catch (_) { /* number inputs */ } }
}
