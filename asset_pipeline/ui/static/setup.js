// Setup (Tools › Setup…, and by itself on first run): storage folder, ComfyUI backend and address,
// the Comfy API proxy, API keys the enabled backends need, Blender, Unreal, GPUs. Each item shows
// its current value (or the detected one, pre-filled) and has a Test that answers in plain words.
// Save writes ~/.config/terraformer/config.toml (keys to secrets.toml). Server: ui/setup_api.py.

async function setupDialog() {
  const body = document.createElement("div"); body.id = "setup-body";
  body.innerHTML = `<p class="hint">Checking this machine…</p>`;
  // until the machine is checked there is nothing to test or save: the buttons wait
  let testAll = async () => {}, save = async () => false;
  const d = dialog({title: "Setup", body, className: "tool-window setup-window", buttons: [
    {label: "Test all", onClick: () => { testAll(); return false; }},
    {label: "Save", default: true, onClick: () => save()},
    {label: "Cancel"}]});
  d.win.id = "setup-dialog";
  const btns = [...d.win.querySelectorAll(".wbuttons button")].slice(0, 2);
  btns.forEach(b => { b.disabled = true; b.title = "Checking this machine…"; });
  let st;
  try { st = await api("/api/setup", undefined, "GET"); }
  catch (err) { body.innerHTML = `<span class="error">${esc(errorText(err))}</span>`; return; }
  const gpuOpts = st.gpus.map(g => `<option value="${g.index}">${g.index}: ${esc(g.name)} (${Math.round(g.vram_gb)} GB)</option>`).join("");
  const input = it => {
    const v = it.value || it.detected || "";
    const id = `su-${it.id}`;
    if (it.kind === "choice") return `<select id="${id}" data-id="${it.id}">${it.choices.map(c => `<option value="${esc(c)}"${c === v ? " selected" : ""}>${esc((it.labels || {})[c] || c)}</option>`).join("")}</select>`;
    if (it.kind === "gpu" && st.gpus.length) return `<select id="${id}" data-id="${it.id}">${gpuOpts}</select>`;
    if (it.kind === "info") return `<input id="${id}" data-id="${it.id}" type="hidden" value=""><span class="hint su-info">Test shows what is installed.</span>`;
    if (it.kind === "secret") return `<input id="${id}" data-id="${it.id}" type="password" autocomplete="off" placeholder="${it.has_value ? "set (leave empty to keep)" : "not set"}">`;
    return `<input id="${id}" data-id="${it.id}" type="text" spellcheck="false" value="${esc(v)}">`;
  };
  body.innerHTML = `${st.first_run ? `<p><b>Welcome to Terraformer.</b> Check these settings (found ones are filled in), test them, and Save.
      You can come back here any time from Tools › Setup….</p>` : ""}
    <p class="hint" style="margin:0 0 6px">Saved to <code>${esc(st.config)}</code>; API keys to <code>${esc(st.secrets)}</code> (only you can read it).
      ${st.gpus.length ? `GPUs: ${st.gpus.map(g => `${g.index} = ${esc(g.name)}`).join(", ")}.` : "No GPU found."}</p>
    <div class="setup-grid">${st.items.map(it => `<div class="setup-item" data-id="${it.id}"${it.shown ? "" : " hidden"}>
      <label for="su-${it.id}"><b>${esc(it.label)}</b>${it.optional ? ` <span class="hint" style="margin:0">optional</span>` : ""}</label>
      <div class="row setup-row">${input(it)}
        ${it.detected && it.value && it.detected !== it.value && it.kind !== "secret" ? `<button type="button" class="small su-detected" title="Found on this machine: ${esc(it.detected)}">Use found</button>` : ""}
        <button type="button" class="small su-test">Test</button></div>
      <div class="hint su-help" style="margin:0">${esc(it.help)}</div>
      <div class="su-result" role="status" aria-live="polite"></div></div>`).join("")}</div>`;
  for (const it of st.items) {
    const el = $(`#su-${it.id}`, body);
    if (it.kind === "gpu" && st.gpus.length) el.value = String(it.value || it.detected || 0);
  }
  const values = () => Object.fromEntries([...body.querySelectorAll("[data-id]")].filter(e => e.matches("input, select") && e.type !== "password")
    .map(e => [e.dataset.id, e.value.trim()]));
  const shown = () => {                               // the sdk-only items follow the backend choice
    const backend = $("#su-comfy_backend", body).value;
    for (const it of st.items) {
      const row = $(`.setup-item[data-id="${it.id}"]`, body);
      if (it.when === "backend=sdk") row.hidden = backend !== "sdk";
      else if (it.when === "backend=httpx") row.hidden = backend !== "httpx";
    }
  };
  $("#su-comfy_backend", body).addEventListener("change", shown);
  // a profile sets its lane defaults (one GPU: both roles on it); the GPU items stay editable
  $("#su-hardware_profile", body).addEventListener("change", e => {
    const p = st.profiles[e.target.value];
    if (!p) return;
    for (const [id, lane] of [["gpu_vision", "gpu0"], ["gpu_comfy", "comfy"]]) {
      const el = $(`#su-${id}`, body);
      if (el && [...(el.options || [])].some(o => o.value === String(p.lanes[lane]))) el.value = String(p.lanes[lane]);
      else if (el) el.value = String(p.lanes[lane]);
    }
    ["hardware_profile", "gpu_vision", "gpu_comfy", "vlm_model"].forEach(test);
  });
  shown();
  const test = async id => {
    const row = $(`.setup-item[data-id="${id}"]`, body), out = $(".su-result", row), el = $(`#su-${id}`, body);
    out.className = "su-result hint"; out.textContent = "Testing…";
    try {
      const r = await api("/api/setup/test", {id, value: el.type === "password" ? el.value : el.value.trim(), values: values()});
      out.className = `su-result ${r.ok ? (r.warn ? "su-warn" : "ok") : "error"}`;
      out.textContent = `${r.ok ? (r.warn ? "Note: " : "✓ ") : "✗ "}${r.message}`;
      return r;
    } catch (err) { out.className = "su-result error"; out.textContent = errorText(err); return {ok: false}; }
  };
  testAll = async () => {
    const ids = st.items.map(it => it.id).filter(id => !$(`.setup-item[data-id="${id}"]`, body).hidden);
    return Promise.all(ids.map(test));
  };
  body.querySelectorAll(".su-test").forEach(b => b.onclick = () => test(b.closest(".setup-item").dataset.id));
  body.querySelectorAll(".su-detected").forEach(b => b.onclick = () => {
    const id = b.closest(".setup-item").dataset.id;
    $(`#su-${id}`, body).value = st.items.find(it => it.id === id).detected;
    test(id);
  });
  save = async () => {
    const vals = values();
    for (const it of st.items) if ($(`.setup-item[data-id="${it.id}"]`, body).hidden) delete vals[it.id];
    const secrets = {};
    body.querySelectorAll("input[type=password]").forEach(e => {
      if (e.value.trim()) secrets[st.items.find(it => it.id === e.dataset.id).key.replace(/^secret:/, "")] = e.value.trim();
    });
    try {
      const r = await api("/api/setup/save", {values: vals, secrets});
      alert(`Saved to ${r.config}. Storage: ${r.storage}.\nTools › Doctor checks everything at once.`);
      await loadProjects(slug || undefined);
    } catch (err) { alert(errorText(err)); return false; }
  };
  btns.forEach(b => { b.disabled = false; b.title = ""; });
  testAll();
}

ACTIONS.setup = {run: setupDialog};
{
  const tools = MENUS.find(m => m[0] === "Tools")[2];
  tools.splice(0, 0, ["Setup…", "setup"]);
}

// First run (no user config yet): open Setup by itself once the app has loaded.
window.addEventListener("load", async () => {
  try {
    const r = await api("/api/setup/first-run", undefined, "GET");
    if (r.first_run && !document.querySelector("#setup-dialog")) setupDialog();
  } catch (_) { /* the app works without it */ }
});
