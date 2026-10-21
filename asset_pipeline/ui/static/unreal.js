// Unreal tab: numbered steps (create the UE project, export, import, render shots, match look),
// each enabled only when it can run, and the UE project backup. Import, renders and look scenes
// open the Unreal menu's dialogs (world.js); all run as CPU-lane jobs.
const UE_JOBS = ["ue.export", "ue.backup"];
let unrealData = null;
const ueExportStale = () => !!(unrealData && unrealData.export && statusData &&
  unrealData.export.mtime + 1 < (statusData.greybox_mtime || 0));

// The Unreal tab's numbered steps: each button is enabled only when its step can run, and says why
// not beside it. [state text, done?, reason it can't run ("" = it can)].
function ueSteps(u) {
  const exp = u.export, imp = u.imported, ren = u.renders, looks = u.looks || [];
  const when = t => new Date(t * 1000).toLocaleString();
  const fresh = exp && !ueExportStale();
  return {
    init: [u.project ? `Created: ${u.uproject}` : "", !!u.project, u.project ? "Already created." : ""],
    export: [null, !!fresh, !u.greybox ? "Build the greybox first (Greybox)." : ""],
    import: [imp ? `Imported ${when(imp.mtime)}${imp.ok ? "" : " (with errors)"}${exp && imp.mtime < exp.mtime ? " · the export is newer: import again" : ""}` : "",
      !!(imp && exp && imp.mtime >= exp.mtime && imp.ok),
      !u.project ? "Create the UE project first (step 1)." : !exp ? "Export first (step 2)." : u.editor_open ? "Close the Unreal Editor first." : ""],
    render: [ren ? `${ren.count} shot render${ren.count === 1 ? "" : "s"}, newest ${when(ren.mtime)}` : "",
      !!(ren && imp && ren.mtime >= imp.mtime), !imp ? "Import first (step 3)." : ""],
    look: [looks.length ? `${looks.length} look preset${looks.length === 1 ? "" : "s"}: ${looks.map(l => `${l.name}${l.runs ? ` (${l.runs} run${l.runs === 1 ? "" : "s"})` : ""}`).join(", ")}`
      : "No look preset yet: one is created from an approved shot.", looks.some(l => l.runs),
      !imp ? "Import first (step 3)." : !looks.length && !(u.approved_shots || []).length ? "Lock a shot on its approved frame first (Frames)." : ""],
  };
}

function renderUnreal() {
  const u = unrealData;
  if (!u) return;
  const steps = ueSteps(u), running = k => activeJobs(k).length > 0;
  for (const [k, [state, done, why]] of Object.entries(steps)) {
    const li = $(`#ue-steps [data-step="${k}"]`), btn = $(".ue-do button", li);
    li.classList.toggle("done", done);
    if (k !== "export") {
      btn.disabled = !!why || running(`ue.${k}`);
      $(".ue-state", li).innerHTML = (state ? esc(state) : "") + (why && k !== "init" ? `<span class="ue-why">${esc(why)}</span>` : "");
    }
  }
  $("#ue-look").textContent = (u.looks || []).length ? "Match look…" : "Create look preset…";
  const exporting = activeJobs("ue.export"), backingUp = activeJobs("ue.backup");
  const e = u.export;
  $("#ue-export-state").innerHTML = !u.greybox ? `<span class="ue-why">${esc(steps.export[2])}</span>` :
    !e ? "Not exported yet." :
    `${e.assets} assets, ${e.slots} slots, ${e.instances.toLocaleString("en")} instances, ${e.shots} shots · ` +
    `${new Date(e.mtime * 1000).toLocaleString()}` +
    (ueExportStale() ? ` · <span class="error">greybox changed since: export again</span>` : "");
  $("#ue-export").disabled = !u.greybox || !!exporting.length;
  $("#ue-export").textContent = exporting.length ? "Exporting…" : e ? "Export again" : "Export to Unreal";
  setStop("#ue-export-stop", exporting);

  $("#ue-project").textContent = u.uproject;
  $("#ue-backup-state").innerHTML = !u.project ? `No UE project yet (Unreal › Create UE project).` :
    (u.backups.length ? `${u.backups.length} snapshot${u.backups.length === 1 ? "" : "s"}, newest ${esc(u.backups.at(-1))}`
      : "No snapshots yet.") + ` · ${esc(u.backup_dir)}` +
    (u.editor_open ? `<br><span class="error">An Unreal Editor has the project open: save and close it before backing up.</span>` : "");
  $("#ue-backup").disabled = !u.project || !!backingUp.length;
  $("#ue-backup").textContent = backingUp.length ? "Backing up…" : "Back up now";
  setStop("#ue-backup-stop", backingUp);
}

async function ueExport() {
  try { await api(`/api/projects/${slug}/export`, {}); } catch (err) { alert(errorText(err)); }
  await load();
}

async function ueBackup(force = false) {
  try { await api(`/api/projects/${slug}/ue/backup`, {force}); }
  catch (err) {
    if (!force && err.message.includes("open:") &&
        confirm(`${errorText(err)}.\n\nBack up anyway? A snapshot taken while the editor saves can be inconsistent.`))
      return ueBackup(true);
    if (force || !err.message.includes("open:")) alert(errorText(err));
  }
  await load();
}

$("#ue-export").onclick = () => ueExport();
$("#ue-init").onclick = () => ACTIONS.ueInit.run();
$("#ue-import").onclick = () => ueImportDialog();
$("#ue-render").onclick = () => ueRenderDialog();
$("#ue-look").onclick = () => ((unrealData && unrealData.looks || []).length ? ueLookDialog() : ueLookNewDialog());

// No look preset yet: make one from an approved shot (locked on a frame), then open Look scene.
function ueLookNewDialog() {
  const shots = (unrealData && unrealData.approved_shots) || [];
  if (!shots.length) { alert("Lock a shot on its approved frame first (Frames › Use this frame for this shot)."); return; }
  const {form} = formDialog({title: "Create a look preset", id: "ue-look-new-dialog",
    html: `<label for="ln-shot">Approved shot:</label><select id="ln-shot">${optionList(shots)}</select>
      <p class="hint" style="grid-column:1/-1">Writes site/look_tests/&lt;shot&gt;.json for that shot; the look scene matches
        light, ground and exposure to its locked frame. Edit the preset later for sun, fog or exposure.</p>`,
    buttons: [{label: "Create", default: true, onClick: async () => {
      try { await api(`/api/projects/${slug}/ue/looks/new`, {shot: $("#ln-shot", form).value}); }
      catch (err) { alert(errorText(err)); return false; }
      await load(); setTimeout(ueLookDialog, 0);
    }}, {label: "Cancel"}]});
}
$("#ue-backup").onclick = () => ueBackup();
