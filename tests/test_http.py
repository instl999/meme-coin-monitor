"""Retries with exponential backoff, JSON-RPC error handling, and transaction-version negotiation."""

import pytest
import requests

from tests.fakes import FakeResponse
from watcher.rpc import HttpClient, HttpError, RpcError, SolanaRPC
from watcher.util import Redactor


class Scripted:
    """A session returning scripted responses (or raising scripted exceptions) in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append(json)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(session, sleeps):
    return HttpClient(session=session, sleep=sleeps.append, redact=Redactor(["secret-key-123"]))


def ok(result):
    return FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": result})


def test_retries_429_with_exponential_backoff_and_honours_retry_after():
    sleeps = []
    session = Scripted(FakeResponse(429, {}), FakeResponse(429, {}, headers={"Retry-After": "7"}), ok(1))
    resp = client(session, sleeps).request("POST", "https://x", what="test")
    assert resp.json()["result"] == 1
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.25  # 1 s base, up to 25% jitter
    assert sleeps[1] >= 7.0          # Retry-After respected


def test_backoff_doubles_and_is_capped():
    http = HttpClient(session=Scripted(), sleep=lambda s: None)
    delays = [http.backoff(attempt) for attempt in range(1, 8)]
    assert 1 <= delays[0] <= 1.25 and 2 <= delays[1] <= 2.5 and 4 <= delays[2] <= 5
    assert all(delay <= 30 * 1.25 for delay in delays)


def test_gives_up_after_the_last_attempt_with_a_clear_message():
    sleeps = []
    session = Scripted(*[FakeResponse(503, {"error": {"message": "overloaded"}})] * 5)
    with pytest.raises(HttpError) as caught:
        client(session, sleeps).request("GET", "https://x", what="DEX Screener token-pairs")
    assert str(caught.value) == "DEX Screener token-pairs: HTTP 503 (overloaded) (gave up after 5 attempts)"
    assert caught.value.status == 503 and len(sleeps) == 4


def test_client_errors_are_not_retried():
    session = Scripted(FakeResponse(400, {"description": "Bad Request: chat not found"}))
    with pytest.raises(HttpError, match="HTTP 400 \\(Bad Request: chat not found\\)"):
        client(session, []).request("POST", "https://x", what="Telegram sendMessage")
    assert len(session.calls) == 1


def test_network_errors_are_retried_and_redacted():
    error = requests.ConnectionError("Max retries exceeded with url: /?api-key=secret-key-123")
    session = Scripted(*[error] * 5)
    with pytest.raises(HttpError) as caught:
        client(session, []).request("POST", "https://x", what="RPC getTokenSupply")
    assert "secret-key-123" not in str(caught.value) and "api-key=***" in str(caught.value)


def test_rpc_retries_transient_json_rpc_errors_but_not_bad_requests():
    sleeps = []
    behind = FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "Node is behind"}})
    rpc = SolanaRPC("https://x", client(Scripted(behind, ok({"value": {"amount": "5", "decimals": 2}})), sleeps))
    assert rpc.token_supply("mint") == (5, 2) and len(sleeps) == 1
    invalid = FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid param"}})
    rpc = SolanaRPC("https://x", client(Scripted(invalid), []))
    with pytest.raises(RpcError, match="error -32602: Invalid param"):
        rpc.token_supply("mint")


def test_public_rpc_rate_limit_explains_how_to_fix_it():
    session = Scripted(*[FakeResponse(429, {"error": {"code": 429, "message": "Too many requests for a specific RPC call"}})] * 5)
    rpc = SolanaRPC("https://api.mainnet-beta.solana.com", client(session, []), public=True)
    with pytest.raises(HttpError) as caught:
        rpc.largest_token_accounts("mint")
    assert "Too many requests" in str(caught.value) and "add HELIUS_API_KEY to .env" in str(caught.value)


def test_get_transaction_negotiates_newer_transaction_versions():
    too_new = FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {
        "code": -32015, "message": "Transaction version (2) is not supported by the requesting client."}})
    session = Scripted(too_new, ok({"slot": 1}))
    rpc = SolanaRPC("https://x", client(session, []))
    assert rpc.transaction("sig") == {"slot": 1}
    assert [call["params"][1]["maxSupportedTransactionVersion"] for call in session.calls] == [1, 2]


def test_bulk_reads_slow_down_when_the_provider_pushes_back_then_recover():
    session = Scripted(FakeResponse(429, {}, headers={"Retry-After": "1"}), *[ok(1)] * 60)
    rpc = SolanaRPC("https://x", client(session, []))
    rpc.limit_rate(8)
    rpc.call("getSlot", [])  # answered after one 429
    rpc.call("getSlot", [])
    assert rpc.max_rps == 4
    for _ in range(50):
        rpc.call("getSlot", [])
    assert rpc.max_rps == 5  # creeping back up, never above the cap
    rpc.limit_rate(None)
    assert rpc.max_rps is None


def test_multiple_accounts_is_batched_by_100():
    session = Scripted(ok({"value": [None] * 100}), ok({"value": [None] * 50}))
    rpc = SolanaRPC("https://x", client(session, []))
    assert len(rpc.multiple_accounts([f"a{i}" for i in range(150)])) == 150
    assert [len(call["params"][0]) for call in session.calls] == [100, 50]
