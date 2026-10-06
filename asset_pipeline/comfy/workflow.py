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
                              "strength": "strength_model"}},  # "model" input
        "depth":  {"node": "32", "optional": true,         # optional node with two outputs:
                   "passthrough": {"0": "positive",        #   each output index is rewired
                                   "1": "negative"},       #   to its own input
                   "also_remove": ["31"],                  #   feeder nodes removed with it
                   "fields": {"strength": "strength",      #   fields on the node itself, or
                              "image": {"node": "31", "input": "image"}}},  # on another node
        "anchor": {"repeat": {"nodes": ["22", "23", "24"],  # a block cloned once per list
                              "chain_in": {"node": "24", "input": "conditioning"},  # item and
                              "out": "24"},                 # chained: item i's chain_in reads
                   "fields": {"image": {"node": "22", "input": "image"},     # item i-1's out;
                              "strength": {"node": "24", "input": "strength"}}}  # [] / None
      }                                                     # -> block removed, bypassed
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
        if isinstance(spec, dict) and "repeat" in spec:
            rep = spec["repeat"]
            refs = [rep["chain_in"]] + list(spec["fields"].values())
            for nid in rep["nodes"] + [rep["out"]]:
                if nid not in graph:
                    raise WorkflowError(f"{name}: repeat {key!r} -> missing node {nid!r}")
            for r in refs:
                if r["node"] not in rep["nodes"]:
                    raise WorkflowError(f"{name}: repeat {key!r} field node {r['node']} not in block")
                if r["input"] not in graph[r["node"]]["inputs"]:
                    raise WorkflowError(
                        f"{name}: binding {key!r} -> node {r['node']} has no input {r['input']!r}")
            continue
        for s in spec if isinstance(spec, list) else [spec]:
            if s["node"] not in graph:
                raise WorkflowError(f"{name}: binding {key!r} -> missing node {s['node']!r}")
            refs = [(s["node"], s["input"])] if "input" in s else \
                [_field_ref(s, f) for f in s.get("fields", {})]
            pt = s.get("passthrough")
            refs += [(s["node"], i) for i in ([pt] if isinstance(pt, str) else (pt or {}).values())]
            for nid in s.get("also_remove", []):
                if nid not in graph:
                    raise WorkflowError(f"{name}: binding {key!r} -> also_remove missing node {nid!r}")
            for nid, inp in refs:
                if nid not in graph:
                    raise WorkflowError(f"{name}: binding {key!r} -> missing node {nid!r}")
                if inp not in graph[nid]["inputs"]:
                    raise WorkflowError(
                        f"{name}: binding {key!r} -> node {nid} has no input {inp!r}")


def _field_ref(spec: dict, field: str) -> tuple[str, str]:
    """(node, input) a field of an optional binding writes to."""
    target = spec["fields"][field]
    return (target["node"], target["input"]) if isinstance(target, dict) else (spec["node"], target)


def _remove_node(graph: dict, node_id: str, passthrough: str | dict) -> None:
    """Delete a node and point everything that consumed it at its passthrough input
    (a dict maps each output index to the input that replaces it)."""
    if isinstance(passthrough, str):
        sources = {None: graph[node_id]["inputs"][passthrough]}
    else:
        sources = {int(o): graph[node_id]["inputs"][i] for o, i in passthrough.items()}
    del graph[node_id]
    for node in graph.values():
        for k, v in node["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and v[0] == node_id:
                src = sources.get(None, sources.get(v[1]))
                if src is None:
                    raise WorkflowError(f"node {node_id} output {v[1]} has no passthrough")
                node["inputs"][k] = list(src)


def _rewire(graph: dict, old: list, new: list) -> None:
    for node in graph.values():
        for k, v in node["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and v[0] == old[0] and v[1] == old[1]:
                node["inputs"][k] = list(new)


def _expand_repeat(graph: dict, spec: dict, items: list[dict] | None) -> None:
    """Replace the template block with one chained copy per item (none -> bypass)."""
    rep, block = spec["repeat"], set(spec["repeat"]["nodes"])
    chain = rep["chain_in"]
    upstream = list(graph[chain["node"]]["inputs"][chain["input"]])
    template = {nid: graph.pop(nid) for nid in rep["nodes"]}
    prev = upstream
    for i, item in enumerate(items or []):
        ids = {nid: f"{nid}_{i}" for nid in block}
        for nid, node in template.items():
            clone = copy.deepcopy(node)
            for k, v in clone["inputs"].items():
                if isinstance(v, list) and len(v) == 2 and v[0] in block:
                    clone["inputs"][k] = [ids[v[0]], v[1]]
            graph[ids[nid]] = clone
        graph[ids[chain["node"]]]["inputs"][chain["input"]] = list(prev)
        for field, ref in spec["fields"].items():
            if field not in item:
                raise WorkflowError(f"repeat item {i} needs field {field!r}")
            graph[ids[ref["node"]]]["inputs"][ref["input"]] = item[field]
        prev = [ids[rep["out"]], 0]
    _rewire(graph, [rep["out"], 0], prev)


def fill(graph: dict, manifest: dict, values: dict) -> dict:
    """Return a copy of graph with values bound. Required bindings left unset keep the
    template's value; unknown keys are an error (catches typos)."""
    bindings = manifest.get("bindings", {})
    unknown = set(values) - set(bindings)
    if unknown:
        raise WorkflowError(f"unknown binding(s): {sorted(unknown)}")
    g = copy.deepcopy(graph)
    for key, value in values.items():
        if isinstance(bindings[key], dict) and "repeat" in bindings[key]:
            _expand_repeat(g, bindings[key], value)
            continue
        for s in bindings[key] if isinstance(bindings[key], list) else [bindings[key]]:
            if s.get("optional"):
                if value is None:
                    _remove_node(g, s["node"], s["passthrough"])
                    for nid in s.get("also_remove", []):
                        g.pop(nid, None)
                    continue
                for field in s["fields"]:
                    if field not in value:
                        raise WorkflowError(f"binding {key!r} needs field {field!r}")
                    nid, inp = _field_ref(s, field)
                    g[nid]["inputs"][inp] = value[field]
            else:
                g[s["node"]]["inputs"][s["input"]] = value
    return g
