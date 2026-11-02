"""Run a user-selected local GGUF only for the lifetime of a conversion."""

from __future__ import annotations

import math
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .config import AIConfig

if TYPE_CHECKING:
    from collections.abc import Iterator


class ModelRuntimeError(RuntimeError):
    """The selected local model could not be loaded."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0917
        raise ModelRuntimeError("Local model health checks must not redirect")


def _model_path(model_file: str | Path) -> Path:
    path = Path(model_file).expanduser()
    if path.suffix.lower() != ".gguf":
        raise ModelRuntimeError("Choose a local .gguf model file")
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ModelRuntimeError("The selected model must be a local .gguf file")
        with path.open("rb") as source:
            magic = source.read(4)
    except OSError as exc:
        raise ModelRuntimeError("The selected model file does not exist or cannot be read") from exc
    if magic != b"GGUF":
        raise ModelRuntimeError("The selected file is not a GGUF model")
    return path


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _log_tail(log_file) -> str:
    # Startup diagnostics contain model-loading messages, before bills are sent.
    log_file.flush()
    log_file.seek(0, 2)
    log_file.seek(max(0, log_file.tell() - 1200))
    return "\n".join(log_file.read().decode("utf-8", errors="replace").splitlines()[-8:])


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            process.kill()
        process.wait(timeout=5)


@contextmanager
def managed_local_model(
    model_file: str | Path,
    config: AIConfig,
    *,
    startup_timeout_seconds: float = 60.0,
) -> Iterator[AIConfig]:
    """Start llama-server on loopback, yield its config, and stop only that child.

    The user supplies a model already on disk. This function never downloads
    weights, changes an existing server, or uses environment HTTP proxies.
    """
    path = _model_path(model_file)
    executable = shutil.which("llama-server")
    if executable is None:
        raise ModelRuntimeError(
            "llama-server is required to open GGUF models. "
            "Install llama.cpp (macOS: brew install llama.cpp), then try again."
        )
    if not isinstance(config.model, str) or not config.model.strip():
        raise ModelRuntimeError("ai.model must be a non-empty model name")
    if not math.isfinite(startup_timeout_seconds) or not 0 < startup_timeout_seconds <= 300:
        raise ModelRuntimeError("Model startup timeout must be between 0 and 300 seconds")

    port = _available_port()
    base_url = f"http://127.0.0.1:{port}"
    command = [
        executable,
        "--model",
        str(path),
        "--alias",
        config.model,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--ctx-size",
        "8192",
        "--reasoning",
        "off",
    ]
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with tempfile.TemporaryFile() as log_file:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        except OSError as exc:
            raise ModelRuntimeError("Could not start llama-server") from exc
        try:
            deadline = time.monotonic() + startup_timeout_seconds
            while True:
                if process.poll() is not None:
                    detail = _log_tail(log_file)
                    raise ModelRuntimeError(
                        "llama-server exited while loading the model"
                        + (f":\n{detail}" if detail else "")
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelRuntimeError(
                        "Timed out loading the local model; check the file and available memory"
                    )
                try:
                    with opener.open(base_url + "/health", timeout=min(1.0, remaining)) as response:
                        ready = response.getcode() == 200
                except (HTTPError, URLError, OSError, TimeoutError):
                    ready = False
                if ready and process.poll() is None:
                    break
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            yield replace(config, base_url=base_url + "/v1")
        finally:
            _stop_process(process)
