"""The arcade builder part (arches on square piers, a walk behind, a gallery over it), the arcade
form (bay and corner modules, arched walls), the colonnade range's arcade, their validation and
the build guidance that switches them on. No Blender."""
import pytest

from asset_pipeline import brief_guide, building_gen as bg, building_parts as bp, library_entries, library_review as lr
from asset_pipeline.building_proc import generate

BAY = {"faces": ["front"], "side": {"pattern": ["solid"], "ground": {"kind": "solid"}, "cornice": None},
       "attic": {"storey_h": 3.2, "setback": 2.0, "side_setback": 0.0}}
LOGGIA = bg.TEMPLATES["arcade"]
RANGE = {"range": {"h": 14.3, "d": 8.0, "arch_h": 10.7, "parapet": 0.9}, "bays": 16, "arcade": LOGGIA}


def _parts(form, dims, feats):
    return generate(form, dims, {}, bg.features(form, feats))


def _openings(parts, mat="base"):
    """Slices of pierced walls over an arched head: their bottom edge slopes (pierced cuts walls
    into 4-point slices at every opening edge)."""
    return [p for p in parts if p["kind"] == "prism" and p["mat"] == mat and len(p["xz"]) == 4
            and abs(p["xz"][0][1] - p["xz"][1][1]) > 1e-3 and min(z for _, z in p["xz"]) > 0.5]


def test_bays_keep_their_rhythm_round_a_corner_pier():
    A = bp.merged(bp.ARCADE, {"bay_m": 4.5, "pier_w": 0.9})
    piers, ops, s = bp.arcade_bays(18.9, A)
    assert len(piers) == 5 and len(ops) == 4 and s == pytest.approx(4.5)
    assert piers[0] == pytest.approx((-9.45, -8.55)) and piers[-1][1] == pytest.approx(9.45)
    assert all(w == pytest.approx(3.6) for _, w in ops)
    # no pier at the left end: the neighbour's corner pier stands just outside, same pitch
    piers2, ops2, s2 = bp.arcade_bays(18.0, A, ends=(False, True))
    assert len(ops2) == 4 and s2 == pytest.approx(4.5) and ops2[0][0] - ops2[0][1] / 2 == pytest.approx(-9.0)
    assert len(piers2) == 4                                  # the corner pier isn't drawn twice


def test_run_has_open_arches_impost_mouldings_and_a_gallery_balustrade():
    A = bg.arcade_spec(LOGGIA)
    parts, H = bp.arcade_run(18.9, A, gallery_storeys=1)
    assert H == pytest.approx(A["h"] + A["gallery"]["storey_h"])
    assert not [p for p in parts if p["mat"] == "glass" and p["kind"] == "prism" and p["y0"] < 0.1]   # open arches
    o = bp._arch_opening("round", 0.0, 3.6, 0.0, A["arch_h"], 0.3, A["h"] - 0.3)
    imposts = [p for p in parts if p["kind"] == "box" and p["mat"] == "trim"
               and p["z1"] == pytest.approx(bp.spring(o)) and p["y0"] < 0]
    assert len(imposts) == 5                                  # one round each pier
    rails = [p for p in parts if p["mat"] == "frame" and p["z0"] >= A["h"] and p["z1"] <= A["h"] + 1.2]
    assert rails                                              # a balustrade in the loggia's openings


def test_segmental_and_square_heads():
    A = bg.arcade_spec({"arch": "seg", "rise": 0.25, "gallery": {"kind": "windowed", "arch": "rect", "storeys": 2}})
    parts, H = bp.arcade_run(13.5, A, gallery_storeys=2)
    o = bp._arch_opening("seg", 0.0, 3.6, 0.0, A["arch_h"], 0.25, A["h"] - 0.3)
    assert o["kind"] == "seg" and o["rise"] == pytest.approx(0.9)
    assert [p for p in parts if p["mat"] == "glass"]          # windowed gallery: glazed
    assert H == pytest.approx(A["h"] + 2 * A["gallery"]["storey_h"])


def test_bay_module_is_one_arch_with_open_walk_ends_and_its_storeys():
    w, d, h = 4.5, 16.0, 20.4
    f = bg.features("arcade", BAY)
    parts = _parts("arcade", [w, d, h], BAY)
    n = bg.gallery_storeys(f, h)
    assert n == round((h - f["arcade"]["h"] - 3.2) / 3.4)
    tops = max(bp.top_of(p) for p in parts)
    assert tops == pytest.approx(f["arcade"]["h"] + n * 3.4 + 3.2, abs=1.0)        # the attic survives the 4.5 m width
    # the walk's two ends: walls across it in the side planes, each with an arch through
    ends = [p for p in _openings(parts) if round(p.get("rz", 0)) % 180 == 90]
    assert ends, "no arch through the walk's end walls"
    side_ground = [p for p in parts if p["kind"] == "prism" and p["mat"] == "base" and round(p.get("rz", 0)) in (90, 270)
                   and max(z for _, z in p["xz"]) <= f["arcade"]["h"] + 1e-6]
    assert side_ground                                        # party walls: solid ground storey behind the walk


def test_corner_turns_round_one_pier_and_has_no_twins():
    feats = {"faces": ["front", "left"]}
    parts = _parts("arcade", [16, 16, 20.4], feats)
    fronts = [p for p in _openings(parts) if round(p.get("rz", 0)) % 360 == 0]
    lefts = [p for p in _openings(parts) if round(p.get("rz", 0)) % 360 == 270]
    assert fronts and lefts
    bp.resolve_coincident(parts)
    assert bp.coincident_area(parts) == []


@pytest.mark.parametrize("dims,feats", [
    ([4.5, 16, 20.4], BAY), ([16, 16, 20.4], {"faces": ["front", "left"]}),
    ([30, 24, 12], {"faces": ["front", "left", "right", "back"], "arcade": LOGGIA}),
    ([30, 24, 14], {"faces": ["right"]}), ([9, 7, 16], {"arcade": {"gallery": None, "back": "wall", "h": 14.8}}),
    ([20, 16, 18], {"faces": ["front", "back"], "hip_roof": {"rise": 2, "over": 0.6}})])
def test_arcade_forms_have_no_twins_after_resolving(dims, feats):
    ps = _parts("arcade", dims, feats)
    bp.resolve_coincident(ps)
    assert bp.coincident_area(ps) == []


def test_a_walk_as_deep_as_the_building_is_clamped():
    parts = _parts("arcade", [9, 6, 16], {"arcade": {"gallery": None, "depth": 6.0}})
    ceil = [p for p in parts if p["kind"] == "box" and p["mat"] == "trim" and p["y1"] - p["y0"] > 2]
    assert ceil and max(p["y1"] for p in ceil) <= 5.0 + 1e-6


def test_colonnade_range_takes_the_arcade_and_its_gallery():
    plain = _parts("arcade_hall", [73.09, 24.91, 14.3], {k: v for k, v in RANGE.items() if k != "arcade"})
    parts = _parts("arcade_hall", [73.09, 24.91, 14.3], RANGE)
    A = bg.arcade_spec(LOGGIA)
    on_piers = lambda ps: [p for p in _openings(ps) if p["y1"] - p["y0"] == pytest.approx(A["pier_w"])]   # noqa: E731
    assert len(on_piers(parts)) > 10 and not on_piers(plain)          # arches on square piers, not the grammar's arcade
    assert max(bp.top_of(p) for p in parts) < max(bp.top_of(p) for p in plain)    # its height is the arcade's
    gal = [p for p in _openings(parts, "wall") if min(z for _, z in p["xz"]) >= A["h"] - 1e-6]
    assert gal                                                          # the gallery's arched openings
    hip = _parts("arcade_hall", [73.09, 24.91, 14.3], RANGE | {"hip_roof": {"rise": 2.0, "over": 0.6}})
    assert [p for p in hip if p["kind"] == "hull"]
    par = A["h"] + A["gallery"]["storey_h"] + sum(c[0] for c in A["cornice"])
    assert not [p for p in hip if p["kind"] == "box" and p["z0"] == pytest.approx(par)]     # no parapet under eaves
    ps = list(parts)
    bp.resolve_coincident(ps)
    assert bp.coincident_area(ps) == []


def test_solid_ground_kind_has_no_openings():
    F = bp.merged(bp.FACADE, {"storeys": 1, "ground": {"kind": "solid"}, "pattern": ["solid"], "cornice": None, "parapet": 0})
    assert not [p for p in bp.facade(12.0, F) if p["mat"] == "glass"]


def test_validation_of_arcade_geometry(monkeypatch):
    layout = type("L", (), {"city": None, "shots": []})()
    monkeypatch.setattr(lr.sw_site, "load_layout", lambda store: layout)
    monkeypatch.setattr(lr, "entry_of", lambda store, asset: None)
    b = {"kind": "building", "form": "arcade", "dims": [16, 16, 20.4], "features": {}}
    assert lr.validate(None, "x", b)["errors"] == []
    low = lr.validate(None, "x", b | {"features": {"arcade": {"arch_h": 1.5}}})["errors"]
    assert any("too low for a round arch" in e for e in low)
    assert lr.validate(None, "x", b | {"features": {"arcade": {"arch_h": 1.5, "arch": "seg"}}})["errors"] == []
    assert any("between piers" in e for e in lr.validate(None, "x", b | {"features": {"arcade": {"bay_m": 1.0}}})["errors"])
    assert any("faces" in e for e in lr.validate(None, "x", b | {"features": {"faces": ["top"]}})["errors"])
    assert any("one of" in e for e in lr.validate(None, "x", b | {"features": {"arcade": {"gallery": {"kind": "porch"}}}})["errors"])
    tall = lr.validate(None, "x", b | {"dims": [16, 16, 40], "features": {"arcade": {"gallery": {"storeys": 1}}}})
    assert any("storeys come to" in w for w in tall["warnings"])
    hall = {"kind": "building", "form": "arcade_hall", "dims": [30, 20, 14], "features": {"arcade": LOGGIA}}
    assert any("colonnade range" in w for w in lr.validate(None, "x", hall)["warnings"])


def test_guidance_offers_the_arcade_switched_off_and_switches_it_on():
    brief = {"kind": "building", "form": "arcade_hall", "dims": [73, 25, 14],
             "features": {"range": RANGE["range"], "bays": 16}}
    off = set()
    vals = brief_guide.values(brief, off)
    assert "features.arcade" in off and "features.arcade.gallery.kind" in vals and "features.arcade.pier_w" in vals
    txt = brief_guide.catalogue_text(vals, off)
    assert "# features.arcade is off" in txt and "features.arcade.arch" in txt and "[round, seg]" in txt
    new = brief_guide.apply_edits(brief, [{"path": "features.arcade.gallery.kind", "after": "windowed"}])
    assert new["features"]["arcade"]["gallery"]["kind"] == "windowed"
    assert new["features"]["arcade"]["pier_w"] == bp.ARCADE["pier_w"]          # the rest from the template
    assert "arcade.gallery" in brief_guide.PROMPT and "switched on" in brief_guide.PROMPT


def test_form_choices_presets_and_suggestions():
    p = bg.preset_for("arcade", [4.5, 16, 20.4])
    assert p["faces"] == ["front"] and 5.0 <= p["arcade"]["h"] <= 7.0 and p["arcade"]["arch_h"] < p["arcade"]["h"]
    assert bg.preset_for("arcade_corner", [16, 16, 20.4])["faces"] == ["front", "left"]
    w = bg.preset_for("arcade_wall", [9, 6, 14])
    assert w["arcade"]["gallery"] is None and w["arcade"]["h"] == pytest.approx(12.8) and w["arcade"]["depth"] == 5.0
    assert library_entries.suggestion({"id": "arcaded-block"}, "arcaded-block-bay") == ("arcade", True)
    assert library_entries.suggestion({"id": "arcaded-block"}, "arcaded-block-corner") == ("arcade_corner", True)
    assert library_entries.suggestion({"id": "wall", "typology": "wall"}) == ("arcade_wall", True)
    assert library_entries.suggestion({"id": "depot-concrete", "typology": "depot"}) == ("cube", False)
    assert library_entries.suggestion({"id": "court-house-slate", "typology": "court_house"})[0] == "perimeter"


def test_guidance_keeps_template_values_that_switch_a_part_on(monkeypatch):
    """Found live (2026-10-17): to switch the grid and the attic on, the vision model restated
    their template values, and the check dropped them as "already that value"."""
    layout = type("L", (), {"city": None, "shots": []})()
    monkeypatch.setattr(lr.sw_site, "load_layout", lambda store: layout)
    monkeypatch.setattr(lr, "entry_of", lambda store, asset: None)
    brief = {"kind": "building", "form": "arcade", "dims": [4.5, 16, 20.4], "features": {}}
    kept, dropped = brief_guide.check(None, "bay", brief, [
        {"path": "features.attic.setback", "after": 2.5, "why": "attic"},
        {"path": "features.arcade.gallery.grid.w", "after": 0.5, "why": "grid"},
        {"path": "features.arcade.gallery.storeys", "after": 3.0, "why": "three floors"},
        {"path": "features.arcade.pier_w", "after": 0.9, "why": "unchanged"}])
    assert [(e["path"], e.get("switch_on")) for e in kept] == [
        ("features.attic.setback", "features.attic"), ("features.arcade.gallery.grid.w", "features.arcade.gallery.grid"),
        ("features.arcade.gallery.storeys", None)]
    assert kept[2]["after"] == 3 and isinstance(kept[2]["after"], int)        # a count stays a whole number
    assert [d["path"] for d in dropped] == ["features.arcade.pier_w"] and dropped[0]["reason"] == "already that value"
    new = brief_guide.apply_edits(brief, kept)
    assert new["features"]["attic"]["storey_h"] == bg.TEMPLATES["attic"]["storey_h"]
    assert new["features"]["arcade"]["gallery"]["grid"] == bg.TEMPLATES["grid"]


def test_taller_arches_take_a_taller_arcade_storey_with_them(monkeypatch):
    """Found in the reroll audit (2026-10-23): "taller arches" became arcade.arch_h up to the storey
    height, validation dropped it (the crown stays 0.3 m under h) and the reroll built nothing of it."""
    layout = type("L", (), {"city": None, "shots": []})()
    monkeypatch.setattr(lr.sw_site, "load_layout", lambda store: layout)
    monkeypatch.setattr(lr, "entry_of", lambda store, asset: None)
    brief = {"kind": "building", "form": "arcade", "dims": [4.5, 16, 20.4], "features": {}}
    A = bg.arcade_spec(bg.form_features("arcade", {})["arcade"])
    kept, dropped = brief_guide.check(None, "bay", brief, [{"path": "features.arcade.arch_h", "after": A["h"], "why": "taller"}])
    assert not dropped
    assert [(e["path"], e["after"]) for e in kept] == [("features.arcade.arch_h", A["h"]), ("features.arcade.h", round(A["h"] + 0.3, 3))]
    assert kept[1]["before"] == A["h"] and kept[1]["why"].startswith("moves with arch_h")
    assert brief_guide.check(None, "bay", brief, [{k: e[k] for k in ("path", "after", "why")} for e in kept])[1] == []   # apply() re-checks
    # an explicit edit of the partner is the artist's: no companion overrides it, the clash is dropped
    kept, dropped = brief_guide.check(None, "bay", brief, [{"path": "features.arcade.arch_h", "after": A["h"], "why": ""},
                                                           {"path": "features.arcade.h", "after": A["h"] - 0.1, "why": ""}])
    assert [d["path"] for d in dropped] == ["features.arcade.arch_h"]
