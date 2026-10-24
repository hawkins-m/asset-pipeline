"""Hardware profiles and the 1-GPU schedule, simulated: fake GPUs, fake VRAM, tiny jobs."""
import threading
import time

import pytest

from asset_pipeline import config, gpu, gpu_policy, jobs, profiles
from asset_pipeline.jobs import JobQueue

TWO = [{"index": 0, "name": "A", "vram_gb": 32.0}, {"index": 1, "name": "B", "vram_gb": 32.0}]
ONE_24 = [{"index": 0, "name": "C", "vram_gb": 24.0}]


@pytest.fixture
def machine(monkeypatch):
    def set_(vendor, gpus):
        monkeypatch.setattr(gpu, "vendor", lambda: vendor)
        monkeypatch.setattr(gpu, "list_gpus", lambda: gpus)
    return set_


def test_detect_picks_the_profile(machine):
    for vendor, gpus, want in [("amd", TWO, "rocm-2gpu"), ("amd", ONE_24, "rocm-1gpu"), ("nvidia", TWO, "nvidia"), (None, [], None)]:
        machine(vendor, gpus)
        assert profiles.detect()["profile"] == want
    assert profiles.lanes("rocm-2gpu", 2) == {"gpu0": 0, "comfy": 1}
    assert profiles.lanes("rocm-1gpu", 1) == {"gpu0": 0, "comfy": 0}
    assert profiles.lanes("nvidia", 1) == {"gpu0": 0, "comfy": 0} and profiles.lanes("nvidia", 2) == {"gpu0": 0, "comfy": 1}
    assert "untested" in profiles.PROFILES["nvidia"].title.lower() and not profiles.PROFILES["nvidia"].tested


def test_the_two_gpu_profile_changes_nothing(machine):
    """This machine's setup: lanes split 0/1, the default allocator, ROCm wheels, the same VRAM needs."""
    machine("amd", TWO)
    config.save_user({"hardware": {"profile": "rocm-2gpu"}, "jobs": {"gpu0_gpu": 0, "comfy_gpu": 1}})
    st = gpu_policy.settings()
    assert st["gpus"] == {"gpu0": 0, "comfy": 1} and not st["shared"] and not profiles.shared()
    assert st["vram_gb"] == {"gpu": 16.0, "comfy": 12.0, "vlm": 28.0}         # TRELLIS from the manifest: 16 as before
    assert gpu_policy.GpuPolicy(cfg=st).vlm_need() == 19.0                    # 32b, fallback 8b: as before
    assert profiles.allocator_env() == {} and profiles.torch_tag() == "rocm7.2"
    q = JobQueue(shared=st["shared"])
    assert q.group("comfy") == ("comfy",) and q.group("gpu0") == ("gpu0",)


def test_allocator_and_torch_per_profile(machine):
    machine("nvidia", TWO)
    config.save_user({"hardware": {"profile": "nvidia"}})
    assert profiles.allocator_env() == {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    assert profiles.torch_tag() == "cu128"
    config.save_user({"hardware": {"profile": "rocm-1gpu", "torch": ""}})
    assert profiles.allocator_env() == {}                                     # silent NaNs on gfx1201: never on ROCm
    assert "silent NaNs" in profiles.summary()["allocator_note"]


def test_vram_floors_switch_off_what_a_small_card_cant_run(machine):
    machine("amd", ONE_24)
    config.save_user({"jobs": {"gpu0_gpu": 0, "comfy_gpu": 0}})
    f = profiles.fit()
    assert f["comfy"]["ok"] and f["trellis"]["ok"] and not f["vlm"]["ok"]
    assert "ap config set local.model 8b" in f["vlm"]["why"]                   # the smaller model is offered
    config.save_user({"local": {"model": "8b"}})
    assert profiles.fit()["vlm"]["ok"]
    machine("amd", [{"index": 0, "name": "D", "vram_gb": 12.0}])
    f = profiles.fit()
    assert not f["comfy"]["ok"] and not f["trellis"]["ok"] and "No vision model fits" in f["vlm"]["why"]


# --- the 1-GPU schedule ---------------------------------------------------------------------------

class FakeVlm:
    def __init__(self, up=False):
        self.st = {"model": "8b", "gpu": 0, "pid": 999999} if up else None
        self.downs = 0

    def state(self):
        return self.st

    def down(self, gpu=None, log=None, **kw):
        self.downs += 1
        self.st = None


def _one_gpu(free, vlm=None):
    cfg = {"cpu_slots": 2, "gpus": {"gpu0": 0, "comfy": 0}, "shared": True,
           "vram_gb": {"gpu": 16.0, "comfy": 12.0, "vlm": 19.0}, "poll_s": 0.05}
    pol = gpu_policy.GpuPolicy(free_fn=free, procs_fn=lambda: {}, vlm_mod=vlm or FakeVlm(), comfy_pid_fn=lambda: None,
                               cfg=cfg, sleep=lambda s: time.sleep(0.01))
    pol.vlm_need = lambda: 19.0
    return pol


def _wait(q, job, t=10):
    end = time.time() + t
    while q.get(job.id).status in ("queued", "running") and time.time() < end:
        time.sleep(0.01)
    return q.get(job.id).status


def test_one_gpu_runs_comfyui_trellis_and_vlm_one_at_a_time_in_submit_order():
    running, seen, lock = [], [], threading.Lock()

    def tiny(name):
        def run():
            with lock:
                running.append(name)
                seen.append((name, len([r for r in running if r != "cpu"])))   # GPU jobs running now
            time.sleep(0.05)
            with lock:
                running.remove(name)
            return name
        return run
    q = JobQueue(policy=_one_gpu(lambda g, b: 30.0), shared=True)
    js = [q.submit("frames.generate", "p", tiny("comfy-1"), lane="comfy"),
          q.submit("3d.trellis", "p", tiny("trellis"), lane="gpu0"),
          q.submit("plan.analyze", "p", tiny("vlm"), lane="gpu0"),
          q.submit("refs.generate", "p", tiny("comfy-2"), lane="comfy"),
          q.submit("site.build", "p", tiny("cpu"), lane="cpu")]
    assert all(_wait(q, j) == "done" for j in js)
    gpu_jobs = [(n, k) for n, k in seen if n != "cpu"]
    assert [n for n, _ in gpu_jobs] == ["comfy-1", "trellis", "vlm", "comfy-2"]      # submit order
    assert all(k == 1 for _, k in gpu_jobs)                                          # never two GPU jobs at once


def test_two_gpus_still_run_comfyui_beside_trellis():
    both = threading.Barrier(2, timeout=5)
    q = JobQueue(policy=None, shared=False)
    a = q.submit("frames.generate", "p", lambda: both.wait(), lane="comfy")
    b = q.submit("3d.trellis", "p", lambda: both.wait(), lane="gpu0")
    assert _wait(q, a) == "done" and _wait(q, b) == "done"                           # they met: concurrent


def test_one_gpu_frees_comfyui_before_trellis_and_the_vlm_before_comfyui():
    vlm = FakeVlm(up=True)
    state = {"comfy_models": True}
    pol = _one_gpu(lambda g, b: 30.0 if not (state["comfy_models"] or vlm.st) else 6.0, vlm)
    freed = []
    pol._free_comfy = lambda job, g: freed.append(g) or state.update(comfy_models=False)
    q = JobQueue(policy=pol, shared=True)
    assert _wait(q, q.submit("3d.trellis", "p", lambda: "ok", lane="gpu0")) == "done"
    assert freed == [0] and vlm.downs == 1                     # the VLM unloaded and ComfyUI's models freed
    vlm.st = {"model": "8b", "gpu": 0, "pid": 999999}
    state["comfy_models"] = False
    assert _wait(q, q.submit("frames.generate", "p", lambda: "ok", lane="comfy")) == "done"
    assert vlm.downs == 2                                      # a ComfyUI job unloads the VLM on the shared GPU


def test_a_comfyui_job_calling_the_vlm_on_one_gpu_does_not_deadlock():
    vlm = FakeVlm()
    q = JobQueue(policy=_one_gpu(lambda g, b: 30.0, vlm), shared=True)

    def guided():
        with jobs.using("gpu0", "vlm"):                        # the job already holds the shared GPU
            return "guided"
    j = q.submit("refs.generate", "p", guided, lane="comfy")
    assert _wait(q, j, t=5) == "done" and q.get(j.id).result == "guided"


def test_shared_jobs_wait_for_vram_with_the_holder_named():
    free = {"gb": 4.0}
    pol = _one_gpu(lambda g, b: free["gb"])
    pol.procs_fn = lambda: {0: {777: 11.0}}
    pol._free_comfy = lambda job, g: None
    q = JobQueue(policy=pol, shared=True)
    j = q.submit("3d.trellis", "p", lambda: "ok", lane="gpu0")
    end = time.time() + 5
    while "Waiting for 16 GB" not in q.get(j.id).note and time.time() < end:
        time.sleep(0.02)
    assert "(PID 777) 11.0 GB" in q.get(j.id).note                # named, never killed
    free["gb"] = 20.0
    assert _wait(q, j) == "done"
