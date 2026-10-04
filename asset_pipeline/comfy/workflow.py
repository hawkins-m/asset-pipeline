"""Fill API-format ComfyUI workflow templates from a bindings manifest.

A template is workflows/<name>.json (exported from ComfyUI with "Save (API Format)").
Its manifest, workflows/<name>.bindings.json, maps logical names to node inputs, so a
stage is tuned by editing the graph in ComfyUI, not code:

    {
      "outputs": ["9"],                                   # nodes whose images are results
      "bindings": {
        "prompt": {"node": "6", "input": "text"},          # set one input
        "seed":   [{"node": "3", "input": "seed"}, ...],   # same value into several inputs
        "lora":   {"node": "12", "optional": true,         # whole node, optional:
                   "passthrough": "model",                 #   None -> node removed and its
                   "fields": {"file": "lora_name",         #   consumers rewired to its
                              "strength": "strength_model"}}  #   "model" input
      }
    }
"""
import copy
import json
from pathlib import Path

from .. import config


class WorkflowError(ValueError):
    pass


def load_template(name: str, directory: Path | None = None) -> tuple[dict, dict]:
    d = directory or config.WORKFLOWS_DIR
    graph = json.loads((d / f"{name}.json").read_text())
    manifest = json.loads((d / f"{name}.bindings.json").read_text())
    validate(graph, manifest, name)
    return graph, manifest


def validate(graph: dict, manifest: dict, name: str = "workflow") -> None:
    """Every binding must point at an existing node and input."""
    for out in manifest.get("outputs", []):
        if out not in graph:
            raise WorkflowError(f"{name}: output node {out!r} not in graph")
    for key, spec in manifest.get("bindings", {}).items():
        for s in spec if isinstance(spec, list) else [spec]:
            node = graph.get(s["node"])
            if node is None:
                raise WorkflowError(f"{name}: binding {key!r} -> missing node {s['node']!r}")
            inputs = [s["input"]] if "input" in s else list(s.get("fields", {}).values())
            if s.get("passthrough"):
                inputs.append(s["passthrough"])
            for inp in inputs:
                if inp not in node["inputs"]:
                    raise WorkflowError(
                        f"{name}: binding {key!r} -> node {s['node']} has no input {inp!r}")


def _remove_node(graph: dict, node_id: str, passthrough: str) -> None:
    """Delete a node and point everything that consumed it at its passthrough input."""
    source = graph[node_id]["inputs"][passthrough]
    del graph[node_id]
    for node in graph.values():
        for k, v in node["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and v[0] == node_id:
                node["inputs"][k] = list(source)


def fill(graph: dict, manifest: dict, values: dict) -> dict:
    """Return a copy of graph with values bound. Required bindings left unset keep the
    template's value; unknown keys are an error (catches typos)."""
    bindings = manifest.get("bindings", {})
    unknown = set(values) - set(bindings)
    if unknown:
        raise WorkflowError(f"unknown binding(s): {sorted(unknown)}")
    g = copy.deepcopy(graph)
    for key, value in values.items():
        for s in bindings[key] if isinstance(bindings[key], list) else [bindings[key]]:
            if s.get("optional"):
                if value is None:
                    _remove_node(g, s["node"], s["passthrough"])
                    continue
                for field, inp in s["fields"].items():
                    if field not in value:
                        raise WorkflowError(f"binding {key!r} needs field {field!r}")
                    g[s["node"]]["inputs"][inp] = value[field]
            else:
                g[s["node"]]["inputs"][s["input"]] = value
    return g
