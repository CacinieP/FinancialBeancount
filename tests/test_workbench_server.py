"""Real loopback HTTP coverage without a model process or external requests."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest

STATEMENT = """交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2026-10-15 10:00:00,商户消费,星巴克,拿铁,支出,30.00,零钱,支付成功,wx-test-001,,
2026-10-15 10:10:00,商户消费,青禾文化,Python入门教材,支出,68.00,零钱,支付成功,wx-test-002,,
"""


class _FakeModelController:
    """Only a fake local controller is constructed by HTTP fixtures."""

    def __init__(self, config):
        self.config = config
        self.load_calls: list[Path] = []
        self.closed = False

    def status(self, force=False):
        return {
            "ready": False,
            "name": "MiniCPM-V 4.7 1B",
            "detail": "Synthetic local health failure; model is not ready",
            "base_url": self.config.base_url,
        }

    def load(self, path):
        self.load_calls.append(Path(path))
        raise RuntimeError("Synthetic local model load failure")

    def close(self):
        self.closed = True


@dataclass
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


class _Client:
    def __init__(self, server):
        self.server = server
        self.host, self.port = server.server_address
        self.token = ""

    def request(self, method, path, payload=None, *, headers=None, raw=None):
        # HTTPConnection is direct; proxy environment variables cannot cause
        # a test request to leave this ephemeral loopback server.
        connection = HTTPConnection(self.host, self.port, timeout=5)
        request_headers = dict(headers or {})
        body = raw
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return _Response(
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def get(self, path, *, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path, payload, *, authenticated=True, headers=None):
        request_headers = dict(headers or {})
        if authenticated:
            request_headers.setdefault("X-Workbench-Token", self.token)
        return self.request("POST", path, payload, headers=request_headers)

    def state(self):
        response = self.get("/api/state")
        assert response.status == 200, response.body
        return response.json()

    def wait_job(self, *, expect_error=False):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.state()
            if not state["job"]["running"]:
                if expect_error:
                    assert state["job"]["error"], state["job"]
                else:
                    assert not state["job"]["error"], state["job"]
                return state
            time.sleep(0.01)
        pytest.fail("Synthetic workbench job did not finish within five seconds")

    def run_job(self, path, payload, *, expect_error=False):
        response = self.post(path, payload)
        assert response.status == 202, response.body
        assert "job" in response.json()
        return self.wait_job(expect_error=expect_error)


def _upload(name, content):
    return {"name": name, "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from beancount_dedup import workbench

    monkeypatch.setattr(workbench, "ModelController", _FakeModelController)
    server = workbench.create_server(tmp_path / "workspace", port=0)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    client = _Client(server)
    client.token = client.state()["csrf_token"]
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert server.model.closed


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "src" in attrs:
            self.paths.append(attrs["src"])
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            self.paths.append(attrs["href"])


def test_homepage_and_its_assets_are_local_and_served(client):
    response = client.get("/")
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/html")
    assets = _Assets()
    assets.feed(response.body.decode("utf-8"))
    assert assets.paths, "The actual workbench page should load its packaged UI assets"
    for path in assets.paths:
        assert not urlsplit(path).netloc, "The offline UI must not load a CDN asset"
        asset = client.get("/" + path.lstrip("/"))
        assert asset.status == 200, path
        assert asset.body
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_constructing_server_does_not_load_a_model_and_failed_health_is_honest(client):
    state = client.state()
    assert client.server.model.load_calls == []
    assert state["model"]["ready"] is False
    assert "not ready" in state["model"]["detail"]
    assert state["transactions"] == []
    assert isinstance(state["csrf_token"], str) and len(state["csrf_token"]) >= 32


@pytest.mark.parametrize("header", [None, "wrong-token"])
def test_mutations_require_the_current_session_token(client, header):
    headers = {} if header is None else {"X-Workbench-Token": header}
    response = client.post("/api/demo", {}, authenticated=False, headers=headers)
    assert response.status == 403
    assert client.state()["transactions"] == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "example.com"},
        {"Host": "127.0.0.1:1"},
        {"Origin": "https://example.com"},
        {"Origin": "http://localhost:1"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_host_and_cross_site_checks_protect_reads_and_mutations(client, headers):
    assert client.get("/api/state", headers=headers).status == 403
    assert client.post("/api/demo", {}, headers=headers).status == 403
    assert client.state()["transactions"] == []


def test_same_origin_mutation_is_accepted(client):
    origin = f"http://127.0.0.1:{client.port}"
    response = client.post("/api/reset", {}, headers={"Origin": origin})
    assert response.status == 202
    client.wait_job()


@pytest.mark.parametrize(
    "path",
    [
        "/../private-ledger.beancount",
        "/%2e%2e/private-ledger.beancount",
        "/static/%2e%2e/private-ledger.beancount",
        "/api/download/unknown-token",
        "/api/download/..%2Fprivate-ledger.beancount",
        "/api/download/%2Fetc%2Fpasswd",
    ],
)
def test_get_routes_cannot_read_arbitrary_files(client, tmp_path, path):
    private = tmp_path / "private-ledger.beancount"
    private.write_text("PRIVATE-LEDGER-SENTINEL", encoding="utf-8")
    response = client.get(path)
    assert response.status in {403, 404}
    assert b"PRIVATE-LEDGER-SENTINEL" not in response.body


@pytest.mark.parametrize(
    "name", ["../escape.csv", "/absolute.csv", "a/../../escape.csv", r"..\escape.csv"]
)
def test_upload_filenames_cannot_escape_the_workspace(client, tmp_path, name):
    response = client.post("/api/import/statements", {"files": [_upload(name, STATEMENT)]})
    if response.status == 202:
        client.wait_job(expect_error=True)
    else:
        assert response.status == 400, response.body
    assert not (tmp_path / "escape.csv").exists()
    assert client.state()["transactions"] == []


def test_uploaded_ledger_cannot_include_files_outside_its_upload(client, tmp_path):
    private = tmp_path / "private.beancount"
    private.write_text("2026-01-01 open Assets:Private CNY\n", encoding="utf-8")
    main = f'include "{private}"\n'
    client.run_job(
        "/api/import/ledger", {"files": [_upload("main.beancount", main)]}, expect_error=True
    )
    assert "Assets:Private" not in json.dumps(client.state())
    assert private.read_text(encoding="utf-8") == "2026-01-01 open Assets:Private CNY\n"


def test_native_ledger_upload_supports_safe_relative_includes(client):
    main = (
        "2026-01-01 open Assets:Wallet CNY\n"
        "2026-01-01 open Expenses:Books CNY\n"
        'include "months/october.beancount"\n'
    )
    month = (
        '2026-10-15 * "Bookshop" "Textbook"\n'
        "  Assets:Wallet -68.00 CNY\n"
        "  Expenses:Books 68.00 CNY\n"
    )
    client.run_job(
        "/api/import/ledger",
        {
            "files": [_upload("main.beancount", main), _upload("months/october.beancount", month)],
            "main": "main.beancount",
        },
    )
    response = client.post("/api/export", {"kind": "native"})
    assert response.status == 200, response.body
    exported = response.json()
    from beancount import loader
    from beancount.core import data

    entries, errors, _ = loader.load_string(exported["content"])
    assert not errors
    assert sum(isinstance(entry, data.Transaction) for entry in entries) == 1
    assert 'include "months/october.beancount"' not in exported["content"]


def test_failed_model_health_blocks_classification_without_faking_results(client):
    client.run_job("/api/import/statements", {"files": [_upload("wechat.csv", STATEMENT)]})
    before = client.state()["transactions"]
    client.run_job("/api/classify", {}, expect_error=True)
    state = client.state()
    assert state["model"]["ready"] is False
    assert state["transactions"] == before
    assert client.server.model.load_calls == []


def test_failed_model_upload_load_is_reported_and_temporary_file_removed(client):
    response = client.request(
        "POST",
        "/api/model/import?filename=synthetic-1b.gguf",
        headers={"X-Workbench-Token": client.token, "Content-Type": "application/octet-stream"},
        raw=b"GGUF" + b"synthetic-test-only",
    )
    assert response.status == 202, response.body
    state = client.wait_job(expect_error=True)
    assert "Synthetic local model load failure" in state["job"]["error"]
    assert state["model"]["ready"] is False
    assert len(client.server.model.load_calls) == 1
    assert not client.server.model.load_calls[0].exists()


def test_model_directory_creation_failure_does_not_block_following_jobs(client):
    blocker = client.server.workspace / "models"
    blocker.write_text("existing-file-must-survive", encoding="utf-8")
    response = client.request(
        "POST",
        "/api/model/import?filename=synthetic-1b.gguf",
        headers={"X-Workbench-Token": client.token, "Content-Type": "application/octet-stream"},
        raw=b"GGUF\x00\x00\x00\x00",
    )
    assert response.status == 400, response.body
    assert client.server.model.load_calls == []
    assert blocker.read_text(encoding="utf-8") == "existing-file-must-survive"
    # Each request uses a fresh connection. A failed upload must release the
    # mutation lock so an unrelated action can still finish normally.
    state = client.run_job("/api/demo", {})
    assert state["transactions"]
    assert state["job"]["running"] is False
    assert state["job"]["error"] is None


def test_successful_model_upload_is_saved_and_restored_without_loading(client, monkeypatch):
    from beancount_dedup import workbench

    def accept_model(path):
        client.server.model.load_calls.append(Path(path))

    monkeypatch.setattr(client.server.model, "load", accept_model)
    original_name = "selected-1b.gguf"
    model_bytes = b"GGUF\x00\x00\x00\x00"
    response = client.request(
        "POST",
        "/api/model/import?filename=" + original_name,
        headers={"X-Workbench-Token": client.token, "Content-Type": "application/octet-stream"},
        raw=model_bytes,
    )
    assert response.status == 202, response.body
    client.wait_job()
    saved = client.server.saved_model_file
    assert saved == client.server.model.load_calls[0]
    assert saved.name == original_name
    assert saved.read_bytes() == model_bytes
    selection = json.loads((client.server.workspace / "selected-model.json").read_text())
    assert client.server.workspace / selection["path"] == saved
    assert not list(client.server.workspace.glob(".selected-model-*.json"))

    restored = workbench.create_server(client.server.workspace, port=0)
    try:
        assert restored.saved_model_file == saved
        assert restored.model.load_calls == []
    finally:
        restored.server_close()


def test_saved_model_selection_outside_workspace_is_ignored(tmp_path, monkeypatch):
    from beancount_dedup import workbench

    monkeypatch.setattr(workbench, "ModelController", _FakeModelController)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"GGUF\x00\x00\x00\x00")
    (workspace / "selected-model.json").write_text(json.dumps({"path": str(outside)}))
    server = workbench.create_server(workspace, port=0)
    try:
        assert server.saved_model_file is None
        assert server.model.load_calls == []
        assert outside.read_bytes() == b"GGUF\x00\x00\x00\x00"
    finally:
        server.server_close()


@pytest.mark.parametrize("name", ["../escape.gguf", "/outside.gguf", r"..\escape.gguf"])
def test_model_upload_cannot_choose_an_arbitrary_filesystem_path(client, name):
    from urllib.parse import quote

    response = client.request(
        "POST",
        "/api/model/import?filename=" + quote(name, safe=""),
        headers={"X-Workbench-Token": client.token, "Content-Type": "application/octet-stream"},
        raw=b"GGUF" + b"synthetic-test-only",
    )
    assert response.status == 400, response.body
    assert client.server.model.load_calls == []


@pytest.mark.parametrize(
    "account", ["expenses:invalid", "Expenses:Books\n2026-01-01 open Assets:Injected CNY"]
)
def test_invalid_manual_account_does_not_change_financial_rows(client, account):
    initial = client.run_job(
        "/api/import/statements", {"files": [_upload("wechat.csv", STATEMENT)]}
    )
    before = initial["transactions"]
    client.run_job(
        "/api/transactions/update",
        {"ids": [before[0]["id"]], "account": account, "confirm": True},
        expect_error=True,
    )
    assert client.state()["transactions"] == before


def test_unknown_transaction_id_rejects_the_whole_batch_update(client):
    initial = client.run_job(
        "/api/import/statements", {"files": [_upload("wechat.csv", STATEMENT)]}
    )
    before = initial["transactions"]
    client.run_job(
        "/api/transactions/update",
        {
            "ids": [before[0]["id"], "does-not-exist"],
            "account": "Expenses:Education:Books",
            "confirm": True,
        },
        expect_error=True,
    )
    assert client.state()["transactions"] == before


def test_import_confirm_and_download_fragment_preserve_actual_beancount_amounts(client):
    state = client.run_job("/api/import/statements", {"files": [_upload("wechat.csv", STATEMENT)]})
    rows = state["transactions"]
    assert len(rows) == 2
    assert all(row["review_status"] == "pending" for row in rows)
    book = next(row for row in rows if row["counterparty"] == "青禾文化")
    coffee = next(row for row in rows if row["counterparty"] == "星巴克")
    client.run_job(
        "/api/transactions/update",
        {"ids": [book["id"]], "account": "Expenses:Education:Books", "confirm": True},
    )
    confirmed = client.run_job("/api/transactions/update", {"ids": [coffee["id"]], "confirm": True})
    assert all(row["review_status"] == "confirmed" for row in confirmed["transactions"])
    assert {row["id"]: row["amount"] for row in rows} == {
        row["id"]: row["amount"] for row in confirmed["transactions"]
    }
    response = client.post(
        "/api/export", {"kind": "fragment", "only_confirmed": True, "exclude_matched": True}
    )
    assert response.status == 200, response.body
    exported = response.json()
    assert exported["transaction_count"] == 2
    text = exported["content"]
    assert " open " not in text
    downloaded = client.get(exported["download_url"])
    assert downloaded.status == 200
    assert downloaded.body.decode("utf-8") == text
    assert "attachment" in downloaded.headers["content-disposition"]

    from beancount import loader
    from beancount.core import data
    from beancount.parser import parser

    entries, errors, _ = parser.parse_string(text)
    assert not errors
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    assert len(transactions) == 2
    assert all(
        sum((posting.units.number for posting in entry.postings), Decimal(0)) == 0
        for entry in transactions
    )
    asset_total = sum(
        (
            posting.units.number
            for entry in transactions
            for posting in entry.postings
            if posting.account.startswith("Assets:")
        ),
        Decimal(0),
    )
    assert asset_total == Decimal("-98.00")
    accounts = sorted({posting.account for entry in transactions for posting in entry.postings})
    declarations = "\n".join(f"2026-10-01 open {account} CNY" for account in accounts)
    _, validation_errors, _ = loader.load_string(declarations + "\n" + text)
    assert not validation_errors


def test_non_loopback_server_binding_is_rejected_before_construction(tmp_path, monkeypatch):
    from beancount_dedup import workbench

    monkeypatch.setattr(workbench, "ModelController", _FakeModelController)
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        workbench.create_server(tmp_path / "workspace", host="0.0.0.0", port=0)
