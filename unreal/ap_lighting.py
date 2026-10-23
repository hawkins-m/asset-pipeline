"""Sun, sky light, fog, clouds and camera exposure from a look preset (editor Python, UE 5.8),
shared by the look scenes (ap_look_test.py: fresh actors in their own level) and the city level
(ap_import.py: the AP_Env_* actors).

spec: {"sun": {"yaw", "pitch", "temperature", "lux"}, "sky": {"intensity", "color"},
       "fog": {"density", "falloff", "start_cm", "color"}, "white_balance": K,
       "exposure_bias": EV or None (None: auto exposure), "clouds": bool}
(look.sun_direction / look.city_lighting build it.)

owned=True (the city level): a setting is written only while it still holds the value the
pipeline wrote last (tag ap:lit:<name>=<value>); once you change it in Unreal it's yours and
later builds keep it (report["lighting_kept"]). A setting never written before is written.
"""
import unreal


def _fmt(v):
    if isinstance(v, (list, tuple)):
        return ",".join(f"{float(x):.4g}" for x in v)
    return f"{float(v):.4g}" if isinstance(v, (int, float)) else str(v)


def _lc(c):
    return [c.r, c.g, c.b]


class Writer:
    """set(actor, name, get, put, value): writes value unless the actor's current value differs
    from the one the pipeline recorded last (owned), and records it."""

    def __init__(self, owned, report):
        self.owned, self.report = owned, report
        report.setdefault("lighting_kept", [])
        report.setdefault("lighting_set", [])

    def set(self, actor, name, get, put, value):
        if value is None:
            return
        tag = f"ap:lit:{name}"
        tags = [str(t) for t in actor.tags]
        last = next((t.split("=", 1)[1] for t in tags if t.startswith(tag + "=")), None)
        if self.owned and last is not None and _fmt(get()) != last:
            self.report["lighting_kept"].append(f"{actor.get_actor_label()}: {name}")
            return
        put(value)
        if self.owned:
            actor.tags = [unreal.Name(t) for t in tags if not t.startswith(tag + "=")] + [unreal.Name(f"{tag}={_fmt(get())}")]
        self.report["lighting_set"].append(name)


def apply(spec, actors, report, owned=False):
    """Configure actors {sun, skylight, fog, post (PostProcessVolume, optional)} from spec."""
    w = Writer(owned, report)
    sun = actors.get("sun")
    s = spec.get("sun") or {}
    if sun is not None:
        lc = sun.light_component
        lc.set_editor_property("atmosphere_sun_light", True)
        lc.set_mobility(unreal.ComponentMobility.MOVABLE)

        def rot():
            r = sun.get_actor_rotation()
            return [r.pitch, r.yaw]
        if s.get("pitch") is not None:
            w.set(sun, "sun_rot", rot, lambda v: sun.set_actor_rotation(unreal.Rotator(0, v[0], v[1]), False),
                  [s["pitch"], s["yaw"]])
        w.set(sun, "sun_lux", lambda: lc.get_editor_property("intensity"), lambda v: lc.set_intensity(float(v)),
              s.get("lux"))
        if s.get("temperature"):
            lc.set_editor_property("use_temperature", True)
            w.set(sun, "sun_temp", lambda: lc.get_editor_property("temperature"),
                  lambda v: lc.set_editor_property("temperature", float(v)), s["temperature"])
    sky = actors.get("skylight")
    k = spec.get("sky") or {}
    if sky is not None:
        lc = sky.light_component
        lc.set_editor_property("real_time_capture", True)
        lc.set_mobility(unreal.ComponentMobility.MOVABLE)
        w.set(sky, "sky_intensity", lambda: lc.get_editor_property("intensity"), lambda v: lc.set_intensity(float(v)),
              k.get("intensity"))
        if k.get("color"):
            w.set(sky, "sky_color", lambda: _lc(lc.get_editor_property("light_color")),
                  lambda v: lc.set_light_color(unreal.LinearColor(*v, 1.0)), list(k["color"]))
    fog = actors.get("fog")
    f = spec.get("fog") or {}
    if fog is not None:
        fc = fog.component
        for key, prop in (("density", "fog_density"), ("falloff", "fog_height_falloff"), ("start_cm", "start_distance")):
            if key in f:
                w.set(fog, f"fog_{key}", lambda prop=prop: fc.get_editor_property(prop),
                      lambda v, prop=prop: fc.set_editor_property(prop, float(v)), f[key])
        if "color" in f:
            for prop in ("fog_inscattering_luminance", "fog_inscattering_color"):
                try:
                    fc.get_editor_property(prop)
                except Exception:
                    continue
                w.set(fog, "fog_color", lambda prop=prop: _lc(fc.get_editor_property(prop)),
                      lambda v, prop=prop: fc.set_editor_property(prop, unreal.LinearColor(*v, 1.0)), list(f["color"]))
                break
    ppv = actors.get("post")
    if ppv is not None:
        ppv.set_editor_property("unbound", True)

        def pp_set(prop, val):
            st = ppv.get_editor_property("settings")
            st.set_editor_property(f"override_{prop}", True)
            st.set_editor_property(prop, val)
            ppv.set_editor_property("settings", st)

        def pp_get(prop):
            return ppv.get_editor_property("settings").get_editor_property(prop)
        if spec.get("exposure_bias") is not None:   # manual exposure (look scenes set it on the paving)
            pp_set("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL)
            pp_set("auto_exposure_apply_physical_camera_exposure", False)
            w.set(ppv, "exposure_bias", lambda: pp_get("auto_exposure_bias"),
                  lambda v: pp_set("auto_exposure_bias", float(v)), spec["exposure_bias"])
        if spec.get("white_balance"):
            w.set(ppv, "white_temp", lambda: pp_get("white_temp"), lambda v: pp_set("white_temp", float(v)),
                  spec["white_balance"])
    return report
