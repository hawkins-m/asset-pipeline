// Requests to the server and the text of their errors. Loaded first; no DOM needed, so
// tests/test_ui_errors.py runs it in node. Every error that reaches an alert or a dialog is
// turned into readable text here: never "[object Object]".

// Readable text for anything thrown or returned as an error: Error, string, FastAPI detail
// (string, {message}, {detail}, a list of pydantic errors), or any other object (as JSON).
function errorText(x) {
  if (x == null || x === "") return "Unknown error";
  if (typeof x === "string") return x;
  if (x instanceof Error) return errorText(x.message);
  if (Array.isArray(x)) return x.map(e => e && typeof e === "object" && e.msg != null
    ? `${(e.loc || []).filter(p => p !== "body").join(" › ")}${e.loc && e.loc.length ? ": " : ""}${e.msg}` : errorText(e)).join("\n");
  if (typeof x === "object") {
    for (const k of ["message", "detail", "error", "msg"]) if (x[k] != null && x[k] !== "") return errorText(x[k]);
    try { return JSON.stringify(x); } catch (_) { return "Unknown error"; }
  }
  return String(x);
}

// The same for window.alert, whatever a caller passes it.
if (typeof window !== "undefined" && typeof window.alert === "function" && !window.alert.apAlert) {
  const plain = window.alert.bind(window);
  window.alert = m => plain(errorText(m));
  window.alert.apAlert = true;
}

async function api(path, body, method = "POST") {
  const r = await fetch(path, body === undefined ? {} : body instanceof FormData ? {method, body} : {
    method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  // a job that needs ComfyUI while it's down: offer to start it, then send the same request
  // again (the server queues it until ComfyUI answers)
  if (r.status === 409 && j && j.detail && j.detail.code === "comfy_down") {
    const started = typeof offerComfyStart === "function" && await offerComfyStart(errorText(j.detail.message));
    if (!started) throw new Error("ComfyUI is not running. Start it from the Status box, then try again.");
    return api(path, body, method);
  }
  if (!r.ok) throw new Error(errorText(j && j.detail != null ? j.detail : j && Object.keys(j).length ? j : null)
                             .replace(/^Unknown error$/, r.statusText || `HTTP ${r.status}`));
  // every job this UI starts gets its progress dialog (loaders.js)
  if (method === "POST" && j && typeof j.id === "number" && j.kind && j.status && typeof openJobDialog === "function")
    setTimeout(() => openJobDialog(j.id), 0);
  return j;
}
