"""Offline browser workbench, served only on the local loopback interface."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import secrets
import signal
import threading
import uuid
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import AppConfig, load_config
from .workbench_model import ModelController
from .workbench_state import WorkbenchState

logger = logging.getLogger(__name__)
_STATIC = Path(__file__).parent / "web"
_JSON_LIMIT = 96 * 1024 * 1024
_MODEL_LIMIT = 2 * 1024 * 1024 * 1024


class WorkbenchHTTPServer(ThreadingHTTPServer):
    """One private, persistent workspace and one serialized mutation job."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, workspace: Path, config: AppConfig):
        self.workspace = workspace.expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state = WorkbenchState(self.workspace, config)
        self.model = ModelController(config.ai)
        self.state.config.ai = self.model.config
        self.saved_model_file = self._saved_model()
        self.csrf_token = secrets.token_urlsafe(32)
        self._action_lock = threading.Lock()
        self._job_lock = threading.RLock()
        self.job = {"running": False, "label": "", "error": None, "message": ""}
        self._cached_state = self.state.snapshot()
        self._downloads: dict[str, tuple[Path, str]] = {}
        super().__init__(address, WorkbenchHandler)

    def _saved_model(self) -> Path | None:
        try:
            selection = json.loads((self.workspace / "selected-model.json").read_text())
            model = (self.workspace / selection["path"]).resolve()
            if model.is_relative_to((self.workspace / "models").resolve()) and model.is_file():
                return model
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def save_model(self, path: Path) -> None:
        temporary = self.workspace / f".selected-model-{uuid.uuid4().hex}.json"
        try:
            temporary.write_text(json.dumps({"path": str(path.relative_to(self.workspace))}))
            temporary.replace(self.workspace / "selected-model.json")
            self.saved_model_file = path
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        with self._job_lock:
            if not self.job["running"]:
                self._cached_state = self.state.snapshot()
            result = dict(self._cached_state)
            result["job"] = dict(self.job)
        result["model"] = self.model.status()
        result["csrf_token"] = self.csrf_token
        return result

    def start_job(self, label: str, operation) -> dict:
        if not self._action_lock.acquire(blocking=False):
            raise BlockingIOError("上一个操作正在进行，请稍候")
        with self._job_lock:
            self.job = {"running": True, "label": label, "error": None, "message": ""}

        def run():
            error = None
            try:
                operation()
            except Exception as exc:
                error = str(exc) or "操作未完成"
                logger.debug("Workbench action failed", exc_info=True)
            finally:
                with self._job_lock:
                    self._cached_state = self.state.snapshot()
                    self.job = {
                        "running": False,
                        "label": label,
                        "error": error,
                        "message": "" if error else f"{label}已完成",
                    }
                self._action_lock.release()

        threading.Thread(target=run, name="workbench-action", daemon=True).start()
        return {"job": dict(self.job)}

    def register_export(self, result: dict) -> dict:
        token = uuid.uuid4().hex
        filename = Path(result["filename"]).name
        directory = self.workspace / "exports"
        directory.mkdir(exist_ok=True)
        target = directory / f"{token}.beancount"
        target.write_text(result["content"], encoding="utf-8")
        self._downloads[token] = (target, filename)
        return {**result, "download_url": f"/api/download/{token}"}

    def server_close(self):
        self.model.close()
        super().server_close()


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        logger.debug(format, *args)

    def _send(self, status: int, body: bytes, content_type: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, value):
        self._send(
            status,
            json.dumps(value, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def _local_request(self, *, mutation: bool = False) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in hosts:
            self._error(403, "此工作台只接受本机访问")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            self._error(403, "已拒绝来自其他网页的请求")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._error(403, "已拒绝跨站请求")
            return False
        if mutation and not secrets.compare_digest(
            self.headers.get("X-Workbench-Token", ""), self.server.csrf_token
        ):
            self._error(403, "页面会话已过期，请刷新后重试")
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/api/state":
            self._json(200, self.server.snapshot())
        elif path == "/api/health":
            self._json(200, {"status": "ok", "service": "financial-beancount-workbench"})
        elif path.startswith("/api/download/"):
            token = path.removeprefix("/api/download/")
            entry = self.server._downloads.get(token)
            if entry is None:
                self._error(404, "导出文件不存在，请重新导出")
                return
            target, filename = entry
            self._send(
                200,
                target.read_bytes(),
                "text/plain; charset=utf-8",
                {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
            )
        else:
            files = {
                "/": "index.html",
                "/index.html": "index.html",
                "/app.css": "app.css",
                "/app.js": "app.js",
            }
            name = files.get(path)
            if name is None or not (_STATIC / name).is_file():
                self._error(404, "页面不存在")
                return
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(200, (_STATIC / name).read_bytes(), content_type + "; charset=utf-8")

    def _read_json(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 JSON")
        length = self._content_length(_JSON_LIMIT)
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("请求内容必须为对象")
        return value

    def _content_length(self, limit: int) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持此上传方式")
        length = int(self.headers.get("Content-Length", "-1"))
        if length < 0:
            raise ValueError("缺少上传长度")
        if length > limit:
            raise OverflowError("文件过大，请拆分后导入")
        return length

    def _model_upload(self):
        name = parse_qs(urlsplit(self.path).query).get("filename", [""])[0]
        if (
            not name
            or Path(name).name != name
            or "\\" in name
            or not name.lower().endswith(".gguf")
        ):
            raise ValueError("请选择本地 1B GGUF 模型文件")
        length = self._content_length(_MODEL_LIMIT)
        if length < 8:
            raise ValueError("模型文件不完整")
        if not self.server._action_lock.acquire(blocking=False):
            raise BlockingIOError("上一个操作正在进行，请稍候")
        target = None
        try:
            directory = self.server.workspace / "models" / uuid.uuid4().hex
            directory.mkdir(parents=True)
            target = directory / name
            with target.open("xb") as destination:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("模型上传被中断，请重试")
                    destination.write(chunk)
                    remaining -= len(chunk)
            with target.open("rb") as model:
                if model.read(4) != b"GGUF":
                    raise ValueError("该文件不是有效的 GGUF 模型")
        except Exception:
            self.close_connection = True
            if target is not None:
                target.unlink(missing_ok=True)
            raise
        finally:
            self.server._action_lock.release()

        def load():
            try:
                self.server.model.load(target)
                self.server.save_model(target)
                self.server.state.config.ai = self.server.model.config
            except Exception:
                target.unlink(missing_ok=True)
                raise

        return self.server.start_job("加载本地模型", load)

    def do_POST(self):
        if not self._local_request(mutation=True):
            self.close_connection = True
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/model/import":
                self._json(202, self._model_upload())
                return
            payload = self._read_json()
            state = self.server.state
            if path == "/api/export":
                if not self.server._action_lock.acquire(blocking=False):
                    raise BlockingIOError("上一个操作正在进行，请稍候")
                try:
                    result = state.export(
                        kind=payload.get("kind", "standalone"),
                        only_confirmed=payload.get("only_confirmed", False),
                        exclude_matched=payload.get("exclude_matched", False),
                    )
                    self._json(200, self.server.register_export(result))
                finally:
                    self.server._action_lock.release()
                return
            operations = {
                "/api/import/statements": (
                    "导入账单",
                    lambda: state.import_statements(payload.get("files", [])),
                ),
                "/api/import/ledger": (
                    "导入 Beancount 账簿",
                    lambda: state.import_ledger(payload.get("files", []), payload.get("main")),
                ),
                "/api/demo": ("载入合成示例", state.load_demo),
                "/api/reconcile": (
                    "核对账簿",
                    lambda: state.reconcile(payload.get("date_window_days", 0)),
                ),
                "/api/transactions/update": (
                    "更新交易",
                    lambda: state.update_transactions(
                        payload.get("ids", []), payload.get("account"), payload.get("confirm", True)
                    ),
                ),
                "/api/accounts": (
                    "更新账户映射",
                    lambda: state.update_accounts(payload.get("wallets", {})),
                ),
                "/api/rules": (
                    "保存分类规则",
                    lambda: state.update_rules(
                        mode=payload.get("mode", "append"),
                        merchant_overrides=payload.get("merchant_overrides", {}),
                        expense_rules=payload.get("expense_rules", {}),
                        income_rules=payload.get("income_rules", {}),
                    ),
                ),
                "/api/reset": ("清空工作区", state.reset),
            }
            if path == "/api/classify":

                def classify():
                    if not self.server.model.status(force=True)["ready"]:
                        raise ValueError("本地 1B 尚未就绪，请先在模型页连接或导入模型")
                    state.config.ai = replace(self.server.model.config, structured_output=True)
                    state.classify()

                operation = ("1B 分类", classify)
            else:
                operation = operations.get(path)
            if operation is None:
                self._error(404, "操作不存在")
                return
            self._json(202, self.server.start_job(*operation))
        except OverflowError as exc:
            self.close_connection = True
            self._error(413, str(exc))
        except BlockingIOError as exc:
            self._error(409, str(exc))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self._error(400, str(exc))
        except Exception:
            logger.exception("Workbench request failed")
            self._error(500, "操作未完成，请检查文件后重试")


def create_server(
    workspace: Path,
    config: AppConfig | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    model_file: Path | None = None,
) -> WorkbenchHTTPServer:
    """Construct without starting an inference process or accepting remote hosts."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("账单工作台仅允许监听本机地址 127.0.0.1")
    server = WorkbenchHTTPServer((host, port), workspace, config or AppConfig())
    server.selected_model_file = model_file
    return server


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="本地 Beancount 账单工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path("workbench-data"))
    parser.add_argument("--config")
    parser.add_argument("--model-file", type=Path)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = create_server(
        args.workspace, load_config(args.config), args.host, args.port, args.model_file
    )
    selected_model = server.saved_model_file or args.model_file
    if selected_model and (
        server.saved_model_file is not None or not server.model.status(force=True)["ready"]
    ):
        try:
            server.model.load(selected_model)
            server.state.config.ai = server.model.config
        except Exception:
            server.server_close()
            raise
    address = f"http://127.0.0.1:{server.server_address[1]}"
    logger.info("账单工作台已启动：%s", address)
    logger.info("数据保存在本机：%s", server.workspace)

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
