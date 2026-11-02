"""Model suggestions stay local, constrained and separate from ledger facts."""

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock
from urllib.error import URLError

import pytest
from beancount_dedup import ai
from beancount_dedup.account_classifier import BeancountAccountClassifier
from beancount_dedup.cli import main
from beancount_dedup.config import AccountConfig, AIConfig
from beancount_dedup.models import Platform, Transaction, TransactionType

from tests.conftest import ALIPAY_CSV_DATA


@pytest.fixture
def tx():
    return Transaction(
        id="private-transaction-id",
        platform=Platform.ALIPAY,
        datetime=datetime(2024, 1, 15, 12),
        amount=Decimal("-9876.54"),
        counterparty="未知商户",
        description="某项服务",
        tx_type=TransactionType.EXPENSE,
        raw_data={"private-account-number": "never-send-this"},
        payment_method="private-payment-method",
        source_file="private-statement.csv",
    )


@pytest.fixture
def config():
    return AIConfig(allowed_accounts=["Expenses:Services", "Income:Services"], min_confidence=0.8)


@pytest.fixture
def fake_client(monkeypatch):
    client = Mock()
    client.suggest.return_value = {
        "account": "Expenses:Services",
        "confidence": 0.9,
        "reason": "服务描述",
    }
    monkeypatch.setattr(ai, "LocalModelClient", Mock(return_value=client))
    return client


@pytest.fixture
def local_server():
    """Serve only synthetic responses on loopback; never contact a real model."""
    state = {"requests": [], "status": 200, "content": '{"account":null,"confidence":0.1}'}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            state["requests"].append({"path": self.path, "body": json.loads(self.rfile.read(size))})
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/redirected"
                )
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {"choices": [{"message": {"content": state["content"]}}]}
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_default_cli_never_constructs_a_model_client(tmp_path, monkeypatch):
    factory = Mock(side_effect=AssertionError("AI must be opt-in"))
    monkeypatch.setattr(ai, "LocalModelClient", factory)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "alipay.csv").write_text(ALIPAY_CSV_DATA, encoding="utf-8")
    main([])
    assert (tmp_path / "output" / "output.beancount").exists()
    factory.assert_not_called()


def test_rules_including_explicit_fallback_accounts_never_request_ai(tx, config, fake_client):
    transactions = [
        replace(tx, counterparty="星巴克"),
        replace(tx, counterparty="特定商家"),
        replace(tx, amount=Decimal("2"), tx_type=TransactionType.INCOME, counterparty="特定公司"),
    ]
    classifier = BeancountAccountClassifier(
        AccountConfig(
            merchant_overrides={"特定商家": "Expenses:Unknown", "特定公司": "Income:Other"}
        )
    )
    result = ai.suggest_classifications(transactions, classifier, config)
    assert [item.status for item in result] == ["rule_matched"] * 3
    fake_client.suggest.assert_not_called()


@pytest.mark.parametrize("kind", [TransactionType.TRANSFER, TransactionType.REFUND])
def test_special_transaction_types_skip_model(kind, tx, config, fake_client):
    result = ai.suggest_classifications(
        [replace(tx, tx_type=kind)], BeancountAccountClassifier(), config
    )
    assert result[0].status == "skipped_special_type"
    fake_client.suggest.assert_not_called()


def test_zero_amount_skips_model(tx, config, fake_client):
    result = ai.suggest_classifications(
        [replace(tx, amount=Decimal("0"))], BeancountAccountClassifier(), config
    )
    assert result[0].status == "skipped_special_type"
    fake_client.suggest.assert_not_called()


def test_valid_suggestion_respects_threshold_and_does_not_change_transaction(
    tx, config, fake_client
):
    before = tx.to_dict()
    result = ai.suggest_classifications([tx], BeancountAccountClassifier(), config)[0]
    assert result.status == "suggested"
    assert result.suggested_account == "Expenses:Services"
    assert result.confidence == 0.9
    assert result.transaction_id == tx.id
    assert tx.to_dict() == before
    assert fake_client.suggest.call_args.args[1] == ["Expenses:Services"]


@pytest.mark.parametrize(
    ("response", "status"),
    [
        ({"account": "Expenses:Services", "confidence": 0.79}, "needs_review"),
        ({"account": None, "confidence": 0.9}, "needs_review"),
        ({"account": "Expenses:MadeUp", "confidence": 0.99}, "invalid_response"),
        ({"account": "Income:Services", "confidence": 0.99}, "invalid_response"),
        (
            {"account": "Expenses:Services\n2024-01-01 open Assets:Injected", "confidence": 1},
            "invalid_response",
        ),
        ({"account": "Expenses:Services", "confidence": float("nan")}, "invalid_response"),
        ({"account": "Expenses:Services", "confidence": True}, "invalid_response"),
        ({"account": "Expenses:Services", "confidence": 1.1}, "invalid_response"),
        ({"account": "Expenses:Services", "confidence": 0.9, "reason": []}, "invalid_response"),
        ({}, "invalid_response"),
    ],
)
def test_uncertain_or_invalid_model_output_is_not_accepted(
    response, status, tx, config, fake_client
):
    fake_client.suggest.return_value = response
    result = ai.suggest_classifications([tx], BeancountAccountClassifier(), config)[0]
    assert result.status == status
    assert tx.amount == Decimal("-9876.54")
    assert tx.status.value == "unique"


def test_service_failure_stops_later_requests(tx, config, fake_client):
    fake_client.suggest.side_effect = URLError("service unavailable")
    result = ai.suggest_classifications(
        [tx, replace(tx, id="second", description="另一项服务")],
        BeancountAccountClassifier(),
        config,
    )
    assert [item.status for item in result] == ["unavailable", "unavailable"]
    assert fake_client.suggest.call_count == 1


def test_request_budget_counts_unique_prompts_and_cache_survives_budget(tx, config, fake_client):
    result = ai.suggest_classifications(
        [
            tx,
            replace(tx, id="second", amount=Decimal("-10")),
            replace(tx, id="third", description="不同"),
        ],
        BeancountAccountClassifier(),
        replace(config, max_requests=1),
    )
    assert [item.status for item in result] == ["suggested", "suggested", "request_limit"]
    assert [item.transaction_id for item in result] == [tx.id, "second", "third"]
    assert fake_client.suggest.call_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1",
        "http://192.168.1.1:8080/v1",
        "http://127.0.0.1.example.com/v1",
        "http://localhost.example.com/v1",
        "file:///tmp/model",
        "http://user:password@127.0.0.1/v1",
        "http://127.0.0.1/v1?secret=value",
        "http://127.0.0.1/v1#fragment",
    ],
)
def test_remote_or_credential_bearing_urls_are_rejected_before_connecting(url, monkeypatch):
    opener = Mock(side_effect=AssertionError("must reject before opening transport"))
    monkeypatch.setattr(ai, "build_opener", opener)
    with pytest.raises(ValueError, match="loopback"):
        ai.LocalModelClient(AIConfig(base_url=url))
    opener.assert_not_called()


def test_actual_local_request_ignores_proxy_and_sends_only_minimum_text(
    local_server, tx, monkeypatch
):
    url, state = local_server
    for name in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    result = ai.LocalModelClient(AIConfig(base_url=url)).suggest(tx, ["Expenses:Services"])
    assert result == {"account": None, "confidence": 0.1}
    assert len(state["requests"]) == 1
    request = state["requests"][0]
    assert request["path"] == "/v1/chat/completions"
    body = request["body"]
    assert "response_format" not in body  # Legacy local endpoints keep their existing contract.
    content = json.loads(body["messages"][1]["content"])
    assert content == {
        "direction": "expense",
        "counterparty": tx.counterparty,
        "description": tx.description,
        "allowed_accounts": ["Expenses:Services"],
    }
    encoded = json.dumps(body)
    for private in (tx.id, str(tx.amount), "never-send-this", tx.payment_method, tx.source_file):
        assert private not in encoded


def test_opt_in_structured_reply_constrains_account_and_required_fields(local_server, tx):
    url, state = local_server
    ai.LocalModelClient(AIConfig(base_url=url, structured_output=True)).suggest(
        tx, ["Expenses:Services"]
    )
    response_format = state["requests"][0]["body"]["response_format"]
    assert response_format["type"] == "json_object"
    schema = response_format["schema"]
    assert schema["properties"]["account"]["enum"] == ["Expenses:Services", None]
    assert set(schema["required"]) == {"account", "confidence", "reason"}
    assert schema["additionalProperties"] is False


def test_redirect_cannot_send_bill_data_to_another_location(local_server, tx):
    url, state = local_server
    state["status"] = 302
    with pytest.raises(ValueError, match="redirects"):
        ai.LocalModelClient(AIConfig(base_url=url)).suggest(tx, ["Expenses:Services"])
    assert [item["path"] for item in state["requests"]] == ["/v1/chat/completions"]


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"account":"Expenses:Services"} trailing'])
def test_malformed_json_from_actual_endpoint_stays_in_review(local_server, content, tx, config):
    url, state = local_server
    state["content"] = content
    result = ai.suggest_classifications(
        [tx], BeancountAccountClassifier(), replace(config, base_url=url)
    )
    assert result[0].status == "invalid_response"
    assert result[0].suggested_account is None
