"""The workbench must keep a working local model when a replacement fails."""

import io
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from beancount_dedup.config import AIConfig
from beancount_dedup.workbench_model import ModelController


def test_external_service_is_reused_and_never_stopped(monkeypatch):
    monkeypatch.setattr(ModelController, "_probe", staticmethod(lambda config: True))
    controller = ModelController(AIConfig())
    assert controller.status(force=True)["ready"]
    monkeypatch.setattr(
        "beancount_dedup.workbench_model.managed_local_model",
        lambda *args: pytest.fail("External model should not be owned"),
    )
    controller.close()
    assert controller.status(force=True)["ready"]


def test_failed_model_replacement_preserves_previous_service(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def managed(path, config):
        events.append("new_started")
        try:
            yield replace(config, base_url="http://127.0.0.1:9999/v1")
        finally:
            events.append("new_stopped")

    monkeypatch.setattr("beancount_dedup.workbench_model.managed_local_model", managed)
    monkeypatch.setattr(
        ModelController, "_probe", staticmethod(lambda config: "9999" not in config.base_url)
    )
    controller = ModelController(AIConfig())
    with pytest.raises(RuntimeError, match="预期"):
        controller.load(tmp_path / "bad.gguf")
    assert controller.config.base_url == "http://127.0.0.1:8080/v1"
    assert controller.status(force=True)["ready"]
    assert events == ["new_started", "new_stopped"]


def test_owned_model_is_stopped_on_close(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def managed(path, config):
        events.append("started")
        try:
            yield replace(config, base_url="http://127.0.0.1:9999/v1")
        finally:
            events.append("stopped")

    monkeypatch.setattr("beancount_dedup.workbench_model.managed_local_model", managed)
    monkeypatch.setattr(ModelController, "_probe", staticmethod(lambda config: True))
    controller = ModelController(AIConfig())
    controller.load(tmp_path / "valid.gguf")
    assert controller.config.base_url.endswith("9999/v1")
    controller.close()
    controller.close()
    assert events == ["started", "stopped"]


@pytest.mark.parametrize(
    "payload", [{}, {"data": None}, {"data": [1]}, {"data": [{"id": "other-model"}]}]
)
def test_wrong_or_malformed_model_response_is_not_ready(monkeypatch, payload):
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("beancount_dedup.workbench_model.build_opener", lambda *args: Opener())
    assert not ModelController(AIConfig()).status(force=True)["ready"]
