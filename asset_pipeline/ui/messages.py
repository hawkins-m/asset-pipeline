"""Error text for the app: the pipeline's messages are shared with the CLI and some name `ap`
commands and --flags. Every job error and API error the UI shows passes through app_text(),
which names the app's menu items, buttons and fields instead. tests/test_ui_errors.py checks
that nothing with "--" survives."""
import re

# Specific CLI phrasings first (longest wins), then single commands and flags.
COMMANDS = [
    (r"ap library review apply \S+ --done[^.;\n]*", "Library › Review round: mark done…"),
    (r"ap library review apply \S+", "Library › Review round: task list…"),
    (r"ap library brief \S+ \S+ \S+ (?:front|\d|all)", "Brief & build (Library › Reference sheets, on the starred sheet)"),
    (r"ap library brief[^.;)\n]*", "Brief & build (Library › Reference sheets, on the starred sheet)"),
    (r"ap library build[^.;)\n]*", "Save & Rebuild in Library › Asset review (or Build all… in Library › Areas…)"),
    (r"ap library trellis[^.;)\n]*", "TRELLIS all… in Library › Areas…"),
    (r"ap library instances[^.;)\n]*", "Instances… in Library › Areas…"),
    (r"ap library sheets[^.;)\n]*", "+2 more in Library › Reference sheets (or Sheets for missing… in Library › Areas…)"),
    (r"ap library plan \S+", "Re-plan (Library › Asset review)"),
    (r"ap library decide[^.;)\n]*", "Manage entry… › Decision"),
    (r"ap library route[^.;)\n]*", "Manage entry… › 3D route"),
    (r"ap area define \S+ \S+(?: --\S+(?: [^-.;)\n][^.;)\n]*)?)*", "New area… in Library › Areas…"),
    (r"ap area status[^.;)\n]*", "Library › Areas…"),
    (r"ap area checklist[^.;)\n]*", "Write checklist in Library › Areas…"),
    (r"ap shots render \S+(?: \S+)?", "Render passes in World › Shots…"),
    (r"ap shots add[^.;)\n]*", "World › Add shot…"),
    (r"ap frames generate[^.;)\n]*", "World › Generate frames…"),
    (r"ap frames lock[^.;)\n]*", "World › Lock a shot…"),
    (r"ap site init --city", "a city layout (City tab)"),
    (r"ap site init[^.;)\n]*", "Start from the starter layout (Site tab)"),
    (r"ap site build[^.;)\n]*", "Pipeline › Rebuild greybox"),
    (r"ap site vegetation[^.;)\n]*", "World › Vegetation slots…"),
    (r"ap comfy start", "Start ComfyUI (Status box)"),
    (r"ap comfy-check|ap comfy status", "Tools › ComfyUI status…"),
    (r"ap preflight(?: --ue)?", "Tools › Preflight…"),
    (r"ap vlm up[^.;)\n]*", "Tools › Start vision LLM"),
    (r"ap ue backup[^.;)\n]*", "Pipeline › Back up Unreal project"),
    (r"ap ue import[^.;)\n]*", "Unreal › Import…"),
    (r"ap export[^.;)\n]*", "Pipeline › Export to Unreal"),
    (r"ap terrain \w+[^.;)\n]*", "the World › Terrain menu"),
]
FLAGS = {
    "form": "Form", "dims": "Size", "view": "View to measure", "force": "Rebuild unchanged",
    "area": "an area (Library › Areas…)", "district": "a district (Library › Areas…)",
    "shots": "shots", "districts": "districts", "unit": "the asset's row", "guidance": "Guidance",
    "ue": "Also require Unreal Engine", "greybox": "Lock greybox", "reset-layout": "Reset layout",
    "only-stale": "Only stale", "prune": "Prune", "expect": "the expected count", "more": "+2 more",
    "renders": "the review renders", "done": "Mark done", "city": "a city layout",
}
_COMMANDS = [(re.compile(p), r) for p, r in COMMANDS]
_FLAG = re.compile(r"(?<![\w-])--([a-z][a-z0-9-]*)(?:[ =](?:NAME|ID|N|SHOT|[A-Z_]+)\b)?")
_LEFTOVER_AP = re.compile(r"\bap [a-z][a-z-]+(?: [a-z][a-z-]+)?\b")


def app_text(msg: str | None) -> str | None:
    """The message in app terms: commands become menu items, --flags become field names."""
    if not msg:
        return msg
    out = msg
    for rx, repl in _COMMANDS:
        out = rx.sub(repl, out)
    out = _FLAG.sub(lambda m: FLAGS.get(m.group(1), m.group(1).replace("-", " ")), out)
    out = _LEFTOVER_AP.sub("the matching menu item (RUNBOOK.md)", out)
    return out.replace("--", "–")
