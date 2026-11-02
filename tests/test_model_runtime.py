"""Local model lifecycle tests without loading weights or opening a real server."""

import subprocess
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest
from beancount_dedup import model_runtime
from beancount_dedup.config import AIConfig
from beancount_dedup.model_runtime import ModelRuntimeError, managed_local_model


@pytest.fixture
def model_file(tmp_path):
    model = tmp_path / "selected model.gguf"
    model.write_bytes(b"GGUF" + b"synthetic-test-header")
    return model


@pytest.fixture
def runtime(monkeypatch):
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    popen = MagicMock(return_value=process)
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.getcode.return_value = 200
    monkeypatch.setattr(model_runtime.shutil, "which", lambda _: "/local/bin/llama-server")
    monkeypatch.setattr(model_runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(model_runtime, "build_opener", MagicMock(return_value=opener))
    monkeypatch.setattr(model_runtime, "_available_port", lambda: 18747)
    return process, popen, opener


def test_selected_file_starts_only_local_child_and_yields_new_config(model_file, runtime):
    process, popen, opener = runtime
    config = AIConfig(model="openbmb/MiniCPM-V-4.7-1B", max_requests=4)
    original_url = config.base_url
    with managed_local_model(str(model_file), config) as active:
        assert active is not config
        assert active.base_url == "http://127.0.0.1:18747/v1"
        assert active.model == config.model
        assert active.max_requests == 4
        assert config.base_url == original_url
        process.terminate.assert_not_called()
    command = popen.call_args.args[0]
    assert command == [
        "/local/bin/llama-server",
        "--model",
        str(model_file.resolve()),
        "--alias",
        config.model,
        "--host",
        "127.0.0.1",
        "--port",
        "18747",
        "--ctx-size",
        "8192",
        "--reasoning",
        "off",
    ]
    assert "shell" not in popen.call_args.kwargs
    assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert popen.call_args.kwargs["stdout"].closed
    assert popen.call_args.kwargs["stderr"] == subprocess.STDOUT
    opener.open.assert_called_once_with("http://127.0.0.1:18747/health", timeout=1.0)
    handlers = model_runtime.build_opener.call_args.args
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], model_runtime._NoRedirect)
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    process.kill.assert_not_called()


def test_conversion_error_still_stops_own_child(model_file, runtime):
    process, _, _ = runtime
    with (
        pytest.raises(ValueError, match="conversion failed"),
        managed_local_model(model_file, AIConfig()),
    ):
        raise ValueError("conversion failed")
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)


def test_unresponsive_child_is_killed_after_terminate(model_file, runtime):
    process, _, _ = runtime
    process.wait.side_effect = [subprocess.TimeoutExpired("llama-server", 5), -9]
    with managed_local_model(model_file, AIConfig()):
        pass
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


def test_early_exit_reports_load_diagnostics_and_reaps_child(model_file, runtime):
    process, popen, _ = runtime

    def exited_process(*args, **kwargs):
        kwargs["stdout"].write(b"synthetic model loader failure\n")
        return process

    popen.side_effect = exited_process
    process.poll.return_value = 1
    with (
        pytest.raises(ModelRuntimeError, match="synthetic model loader failure"),
        managed_local_model(model_file, AIConfig()),
    ):
        pytest.fail("An exited server must not be used")
    process.terminate.assert_not_called()
    process.wait.assert_called_once_with(timeout=5)


def test_loading_health_errors_retry_then_succeed(model_file, runtime, monkeypatch):
    _, _, opener = runtime
    healthy_response = opener.open.return_value
    opener.open.side_effect = [
        URLError("not listening yet"),
        HTTPError("http://127.0.0.1/health", 503, "loading", {}, None),
        healthy_response,
    ]
    monkeypatch.setattr(model_runtime.time, "sleep", lambda _: None)
    with managed_local_model(model_file, AIConfig()):
        pass
    assert opener.open.call_count == 3


def test_startup_timeout_stops_child(model_file, runtime, monkeypatch):
    process, _, opener = runtime
    opener.open.side_effect = URLError("not ready")
    monkeypatch.setattr(model_runtime.time, "monotonic", MagicMock(side_effect=[0, 0, 1, 1]))
    monkeypatch.setattr(model_runtime.time, "sleep", lambda _: None)
    with (
        pytest.raises(ModelRuntimeError, match="Timed out"),
        managed_local_model(model_file, AIConfig(), startup_timeout_seconds=1),
    ):
        pytest.fail("An unready server must not be used")
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)


def test_health_redirect_is_rejected_and_child_stopped(model_file, runtime):
    process, _, opener = runtime
    opener.open.side_effect = ModelRuntimeError("must not redirect")
    with (
        pytest.raises(ModelRuntimeError, match="must not redirect"),
        managed_local_model(model_file, AIConfig()),
    ):
        pytest.fail("A redirect must not be followed")
    process.terminate.assert_called_once()
    with pytest.raises(ModelRuntimeError, match="must not redirect"):
        model_runtime._NoRedirect().redirect_request(None, None, 302, "", {}, "https://remote")


@pytest.mark.parametrize("filename,content", [("model.txt", b"GGUF"), ("model.gguf", b"HTML")])
def test_wrong_format_rejected_before_process_start(tmp_path, runtime, filename, content):
    _, popen, _ = runtime
    model = tmp_path / filename
    model.write_bytes(content)
    with (
        pytest.raises(ModelRuntimeError, match=r"GGUF|gguf"),
        managed_local_model(model, AIConfig()),
    ):
        pytest.fail("Invalid model file must not load")
    popen.assert_not_called()


def test_missing_file_rejected(tmp_path, runtime):
    _, popen, _ = runtime
    with (
        pytest.raises(ModelRuntimeError, match="does not exist"),
        managed_local_model(tmp_path / "missing.gguf", AIConfig()),
    ):
        pytest.fail("Missing model must not load")
    popen.assert_not_called()


def test_missing_server_shows_install_hint(model_file, runtime, monkeypatch):
    _, popen, _ = runtime
    monkeypatch.setattr(model_runtime.shutil, "which", lambda _: None)
    with (
        pytest.raises(ModelRuntimeError, match=r"brew install llama\.cpp"),
        managed_local_model(model_file, AIConfig()),
    ):
        pytest.fail("Missing server must not load")
    popen.assert_not_called()


def test_spawn_failure_is_readable(model_file, runtime):
    _, popen, _ = runtime
    popen.side_effect = OSError("unavailable binary")
    with (
        pytest.raises(ModelRuntimeError, match="Could not start"),
        managed_local_model(model_file, AIConfig()),
    ):
        pytest.fail("Failed spawn must not yield")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301])
def test_invalid_startup_timeout_rejected_before_start(model_file, runtime, timeout):
    _, popen, _ = runtime
    with (
        pytest.raises(ModelRuntimeError, match="startup timeout"),
        managed_local_model(model_file, AIConfig(), startup_timeout_seconds=timeout),
    ):
        pytest.fail("Invalid timeout must not start")
    popen.assert_not_called()


def test_invalid_model_name_rejected_before_start(model_file, runtime):
    _, popen, _ = runtime
    with (
        pytest.raises(ModelRuntimeError, match="non-empty"),
        managed_local_model(model_file, AIConfig(model="")),
    ):
        pytest.fail("Invalid alias must not start")
    popen.assert_not_called()
