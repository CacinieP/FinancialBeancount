"""Local model lifecycle for the browser workbench."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .ai import validate_ai_config
from .config import AIConfig
from .model_runtime import managed_local_model


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ModelController:
    """Reuse an existing local 1B service or own one selected GGUF process."""

    def __init__(self, config: AIConfig):
        validate_ai_config(config)
        self._config = replace(config)
        self._context = None
        self._lock = threading.RLock()
        self._checked_at = 0.0
        self._cached = self._offline()
        self._filename = ""

    @property
    def config(self) -> AIConfig:
        return replace(self._config)

    def _offline(self) -> dict:
        return {
            "ready": False,
            "name": "MiniCPM-V 4.7 1B",
            "detail": "尚未连接本地模型，可选择 1B GGUF 文件",
            "base_url": self._config.base_url,
        }

    @staticmethod
    def _probe(config: AIConfig) -> bool:
        try:
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            with opener.open(config.base_url.rstrip("/") + "/models", timeout=1) as response:
                payload = json.loads(response.read(65536))
            return any(
                isinstance(model, dict) and model.get("id") == config.model
                for model in payload["data"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def status(self, force: bool = False) -> dict:
        if not self._lock.acquire(blocking=False):
            return {**self._cached, "ready": False, "detail": "正在加载本地模型"}
        try:
            if not force and time.monotonic() - self._checked_at < 3:
                return dict(self._cached)
            result = self._offline()
            if self._probe(self._config):
                result.update(ready=True, detail=(self._filename or "已连接本机 1B 服务"))
            self._cached = result
            self._checked_at = time.monotonic()
            return dict(result)
        finally:
            self._lock.release()

    def load(self, model_file: str | Path) -> None:
        """Replace only a model service owned by this workbench."""
        with self._lock:
            context = managed_local_model(model_file, self._config)
            config = context.__enter__()
            if not self._probe(config):
                context.__exit__(None, None, None)
                raise RuntimeError("模型已启动，但没有返回预期的 1B 服务，请检查模型文件")
            old_context = self._context
            self._context = context
            self._config = config
            self._filename = Path(model_file).name
            self._checked_at = 0
            if old_context is not None:
                old_context.__exit__(None, None, None)

    def close(self) -> None:
        with self._lock:
            if self._context is not None:
                context, self._context = self._context, None
                context.__exit__(None, None, None)
            self._checked_at = 0
