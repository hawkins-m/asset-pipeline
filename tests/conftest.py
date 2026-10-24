"""Every test runs with its own empty user config folder: the machine's
~/.config/terraformer (storage folder, GPUs, API keys) never leaks into a test."""
import pytest

from asset_pipeline import config

_BACKENDS = config.backends          # the cached original (some tests swap in a plain function)


@pytest.fixture(autouse=True)
def _own_user_config(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("AP_CONFIG_DIR", str(tmp_path_factory.mktemp("user-config")))
    for env in config.ENV_OVERRIDES:
        monkeypatch.delenv(env, raising=False)
    _BACKENDS.cache_clear()
    yield
    _BACKENDS.cache_clear()
