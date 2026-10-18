// Keyed redraws for the long lists (Views and 3D, Asset review, Reference sheets): while jobs run
// the page reloads its data every ~2 s, and rebuilding every row made images blank and the layout
// jump. keyedList keeps a row's DOM when its data hasn't changed; a changed row is rebuilt but takes
// over the old row's loaded media (img / model-viewer showing the same file), and a file whose
// version changed is swapped in only once it has loaded. Unchanged rows never move, so the scroll
// position stays put. Loaded after app.js (fileUrl), before the lists' scripts.

const mediaPath = src => String(src || "").replace(/[?#].*$/, "");

// put `fresh` in place of `stale`, keeping its loaded state: same file -> keep it as it is;
// same file, new version -> keep showing the old one until the new one has loaded
function adoptMedia(oldRow, newRow) {
  const pool = new Map();
  oldRow.querySelectorAll("img[src], model-viewer[src]").forEach(m => {
    const k = `${m.tagName}|${mediaPath(m.getAttribute("src"))}`;
    if (!pool.has(k)) pool.set(k, m);
  });
  newRow.querySelectorAll("img[src], model-viewer[src]").forEach(n => {
    const k = `${n.tagName}|${mediaPath(n.getAttribute("src"))}`;
    const o = pool.get(k);
    if (!o) return;
    pool.delete(k);
    const want = n.getAttribute("src");
    for (const a of n.getAttributeNames()) if (a !== "src" && n.getAttribute(a) !== o.getAttribute(a)) o.setAttribute(a, n.getAttribute(a));
    n.replaceWith(o);
    if (o.getAttribute("src") !== want) swapWhenLoaded(o, want);
  });
}

function swapWhenLoaded(el, src) {
  if (el.tagName === "IMG") {
    const pre = new Image();
    pre.onload = () => { el.src = src; };
    pre.src = src;
  } else {
    el.setAttribute("src", src);            // model-viewer keeps the current model until the new one is ready
  }
}

// container: holds one element per item (and nothing else). key(item) -> string; sig(item) ->
// string of everything the row shows; build(item) -> a new element.
function keyedList(container, items, {key, sig, build}) {
  const old = new Map();
  for (const el of [...container.children]) {
    if (el.dataset.key != null) old.set(el.dataset.key, el); else el.remove();
  }
  const next = items.map(it => {
    const k = String(key(it)), s = sig(it);
    let el = old.get(k);
    if (!el || el.dataset.sig !== s) {
      const fresh = build(it);
      fresh.dataset.key = k; fresh.dataset.sig = s;
      if (el) { adoptMedia(el, fresh); el.replaceWith(fresh); }
      el = fresh;
    }
    old.delete(k);
    return el;
  });
  for (const el of old.values()) el.remove();
  next.forEach((el, i) => { if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null); });
  return next;
}

// one section per group (e.g. a plan or a district): a heading (updated in place) over a keyed row
// list, so a changed count in the heading never rebuilds the rows
function keyedSections(container, groups, {heading, cls = "", rowsCls = "", ...rows}) {
  keyedList(container, groups, {
    key: g => g.key,
    sig: () => "",
    build: () => {
      const sec = document.createElement("section"); sec.className = cls;
      const h = document.createElement("div"); h.className = "keyed-head";
      const body = document.createElement("div"); body.className = `keyed-rows ${rowsCls}`.trim();
      sec.append(h, body);
      return sec;
    },
  }).forEach((sec, i) => {
    const h = sec.querySelector(":scope > .keyed-head"), html = heading(groups[i]);
    if (h.dataset.html !== html) { h.innerHTML = html; h.dataset.html = html; }
    keyedList(sec.querySelector(":scope > .keyed-rows"), groups[i].items, rows);
  });
}
