import json

import pytest

from asset_pipeline import config
from asset_pipeline.comfy import workflow
from asset_pipeline.comfy.workflow import WorkflowError

GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x"}},
    "2": {"class_type": "LoraLoaderModelOnly",
          "inputs": {"model": ["1", 0], "lora_name": "", "strength_model": 1.0}},
    "3": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "seed": 0, "steps": 20}},
    "4": {"class_type": "Other", "inputs": {"model": ["2", 0]}},
}
MANIFEST = {
    "outputs": ["3"],
    "bindings": {
        "seed": [{"node": "3", "input": "seed"}],
        "steps": {"node": "3", "input": "steps"},
        "lora": {"node": "2", "optional": True, "passthrough": "model",
                 "fields": {"file": "lora_name", "strength": "strength_model"}},
    },
}


def test_fill_sets_values_and_leaves_template_untouched():
    g = workflow.fill(GRAPH, MANIFEST, {"seed": 7, "steps": 30})
    assert g["3"]["inputs"]["seed"] == 7 and g["3"]["inputs"]["steps"] == 30
    assert GRAPH["3"]["inputs"]["seed"] == 0


def test_optional_node_bound():
    g = workflow.fill(GRAPH, MANIFEST, {"lora": {"file": "style.safetensors", "strength": 0.8}})
    assert g["2"]["inputs"]["lora_name"] == "style.safetensors"
    assert g["2"]["inputs"]["strength_model"] == 0.8


def test_optional_node_removed_and_all_consumers_rewired():
    g = workflow.fill(GRAPH, MANIFEST, {"lora": None})
    assert "2" not in g
    assert g["3"]["inputs"]["model"] == ["1", 0]
    assert g["4"]["inputs"]["model"] == ["1", 0]


def test_unknown_binding_is_an_error():
    with pytest.raises(WorkflowError, match="unknown"):
        workflow.fill(GRAPH, MANIFEST, {"sede": 1})


def test_optional_node_missing_field_is_an_error():
    with pytest.raises(WorkflowError, match="strength"):
        workflow.fill(GRAPH, MANIFEST, {"lora": {"file": "a"}})


def test_validate_catches_bad_input_name():
    bad = {"bindings": {"seed": {"node": "3", "input": "sead"}}}
    with pytest.raises(WorkflowError, match="sead"):
        workflow.validate(GRAPH, bad)


@pytest.mark.parametrize("path", sorted(config.WORKFLOWS_DIR.glob("*.bindings.json")))
def test_repo_templates_are_consistent(path):
    name = path.name.removesuffix(".bindings.json")
    graph, manifest = workflow.load_template(name)
    # every link points at an existing node
    for nid, node in graph.items():
        for v in node["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                assert v[0] in graph, f"{name}: node {nid} links to missing {v[0]}"
    json.dumps(workflow.fill(graph, manifest, {}))


REPEAT_GRAPH = {
    "1": {"class_type": "Guide", "inputs": {}},
    "a": {"class_type": "LoadImage", "inputs": {"image": ""}},
    "b": {"class_type": "Apply", "inputs": {"conditioning": ["1", 0], "img": ["a", 0], "s": 1.0}},
    "k": {"class_type": "KSampler", "inputs": {"positive": ["b", 0]}},
}
REPEAT_MANIFEST = {"bindings": {"anchor": {
    "repeat": {"nodes": ["a", "b"], "chain_in": {"node": "b", "input": "conditioning"}, "out": "b"},
    "fields": {"image": {"node": "a", "input": "image"}, "strength": {"node": "b", "input": "s"}}}}}


def test_repeat_expands_and_chains():
    g = workflow.fill(REPEAT_GRAPH, REPEAT_MANIFEST,
                      {"anchor": [{"image": "x.png", "strength": 0.3}, {"image": "y.png", "strength": 0.3}]})
    assert "a" not in g and "b" not in g
    assert g["b_0"]["inputs"]["img"] == ["a_0", 0] and g["b_1"]["inputs"]["img"] == ["a_1", 0]
    assert g["b_0"]["inputs"]["conditioning"] == ["1", 0]
    assert g["b_1"]["inputs"]["conditioning"] == ["b_0", 0]
    assert g["k"]["inputs"]["positive"] == ["b_1", 0]
    assert g["a_1"]["inputs"]["image"] == "y.png"


@pytest.mark.parametrize("items", [[], None])
def test_repeat_empty_bypasses_block(items):
    g = workflow.fill(REPEAT_GRAPH, REPEAT_MANIFEST, {"anchor": items})
    assert set(g) == {"1", "k"}
    assert g["k"]["inputs"]["positive"] == ["1", 0]


def test_repeat_item_missing_field_is_an_error():
    with pytest.raises(WorkflowError, match="strength"):
        workflow.fill(REPEAT_GRAPH, REPEAT_MANIFEST, {"anchor": [{"image": "x.png"}]})
