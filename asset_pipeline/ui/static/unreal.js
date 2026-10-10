// Unreal tab: export the greybox for UE (stand-in GLBs + manifest) and back up the UE
// project. Import, renders and layout read-back drive UnrealEditor and stay on the command
// line for now. Both actions are CPU-lane jobs with their own progress dialog.
const UE_JOBS = ["ue.export", "ue.backup"];
let unrealData = null;
const ueExportStale = () => !!(unrealData && unrealData.export && statusData &&
  unrealData.export.mtime + 1 < (statusData.greybox_mtime || 0));

function renderUnreal() {
  const u = unrealData;
  if (!u) return;
  const exporting = activeJobs("ue.export"), backingUp = activeJobs("ue.backup");
  const e = u.export;
  $("#ue-export-state").innerHTML = !u.greybox ? "No greybox yet: build it in the City tab first." :
    !e ? "Not exported yet." :
    `${e.assets} assets, ${e.slots} slots, ${e.instances.toLocaleString("en")} instances, ${e.shots} shots · ` +
    `${new Date(e.mtime * 1000).toLocaleString()}` +
    (ueExportStale() ? ` · <span class="error">greybox changed since: export again</span>` : "");
  $("#ue-export").disabled = !u.greybox || !!exporting.length;
  $("#ue-export").textContent = exporting.length ? "Exporting…" : e ? "Export again" : "Export to Unreal";
  setStop("#ue-export-stop", exporting);

  $("#ue-project").textContent = u.uproject;
  $("#ue-backup-state").innerHTML = !u.project ? `No UE project yet (<code>ap ue init PROJECT</code>).` :
    (u.backups.length ? `${u.backups.length} snapshot${u.backups.length === 1 ? "" : "s"}, newest ${esc(u.backups.at(-1))}`
      : "No snapshots yet.") + ` · ${esc(u.backup_dir)}` +
    (u.editor_open ? `<br><span class="error">An Unreal Editor has the project open: save and close it before backing up.</span>` : "");
  $("#ue-backup").disabled = !u.project || !!backingUp.length;
  $("#ue-backup").textContent = backingUp.length ? "Backing up…" : "Back up now";
  setStop("#ue-backup-stop", backingUp);
}

async function ueExport() {
  try { await api(`/api/projects/${slug}/export`, {}); } catch (err) { alert(err.message); }
  await load();
}

async function ueBackup(force = false) {
  try { await api(`/api/projects/${slug}/ue/backup`, {force}); }
  catch (err) {
    if (!force && err.message.includes("open:") &&
        confirm(`${err.message}.\n\nBack up anyway? A snapshot taken while the editor saves can be inconsistent.`))
      return ueBackup(true);
    if (force || !err.message.includes("open:")) alert(err.message);
  }
  await load();
}

$("#ue-export").onclick = () => ueExport();
$("#ue-backup").onclick = () => ueBackup();
