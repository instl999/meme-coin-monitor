"""HTTP with retry/backoff, and a small read-only Solana JSON-RPC client."""

from __future__ import annotations

import itertools
import logging
import math
import random
import re
import time
from urllib.parse import urlsplit

import requests

from . import __version__
from .txparse import signature as tx_signature
from .util import Redactor, clean_text

log = logging.getLogger("holder_watch.http")

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# JSON-RPC errors worth retrying: rate limits, node behind / slot not available yet, internal errors.
RETRY_RPC_CODES = frozenset({429, -32004, -32005, -32007, -32009, -32014, -32016, -32603})
PUBLIC_RPC_HINT = ("The free public Solana RPC throttles this call (getTokenLargestAccounts in particular); "
                   "add HELIUS_API_KEY to .env - free key at https://dashboard.helius.dev")


class HttpError(Exception):
    def __init__(self, what: str, message: str, status: int | None = None):
        super().__init__(f"{what}: {message}")
        self.what = what
        self.message = message
        self.status = status


class RpcError(Exception):
    def __init__(self, method: str, code, message: str):
        super().__init__(f"RPC {method}: error {code}: {message}")
        self.method = method
        self.code = code
        self.message = message


class HttpClient:
    """requests wrapper with timeouts and exponential backoff on HTTP 429/5xx and network errors.

    Error messages name the request (`what`), never the URL, because URLs can carry API keys.
    """

    def __init__(self, session=None, *, attempts=5, base_delay=1.0, max_delay=30.0, timeout=(10, 30),
                 sleep=time.sleep, redact=None):
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = f"holder-watch/{__version__} (read-only token monitor)"
        self.session = session
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.sleep = sleep
        self.redact = redact or Redactor()
        self.rate_limited = 0  # HTTP 429 replies seen; SolanaRPC slows its bulk reads down when this grows

    def backoff(self, attempt: int, retry_after: float | None = None) -> float:
        delay = min(self.max_delay, self.base_delay * 2 ** (attempt - 1)) * (1 + random.random() / 4)
        if retry_after is not None:
            delay = max(delay, min(retry_after, 60.0))
        return delay

    def request(self, method: str, url: str, *, what: str, json=None, attempts: int | None = None):
        """attempts: overrides the client's, e.g. 1 for an API whose rate limit the caller paces itself."""
        attempts = attempts or self.attempts
        error = None
        for attempt in range(1, attempts + 1):
            retry_after = None
            try:
                resp = self.session.request(method, url, json=json, timeout=self.timeout)
            except requests.RequestException as exc:
                error = HttpError(what, f"network error: {self.redact(exc)[:200]}")
            else:
                if resp.status_code < 400:
                    return resp
                error = HttpError(what, f"HTTP {resp.status_code}{_detail(resp)}", resp.status_code)
                if resp.status_code not in RETRY_STATUSES:
                    raise error
                self.rate_limited += resp.status_code == 429
                retry_after = _retry_after(resp)
            if attempt < attempts:
                delay = self.backoff(attempt, retry_after)
                log.debug("%s; retry %d/%d in %.1fs", error, attempt, attempts - 1, delay)
                self.sleep(delay)
        if attempts == 1:
            raise error
        raise HttpError(what, f"{error.message} (gave up after {attempts} attempts)", error.status)


class Pacer:
    """Requests to an API with a per-minute limit: spaced `interval` seconds apart, one try each (every
    request counts against the limit), and a refusal (HTTP 429) waits out the minute before retrying."""

    def __init__(self, http, interval: float, *, limit_wait: float = 61.0, attempts: int = 3, clock=time.monotonic):
        self.http = http
        self.interval = interval
        self.limit_wait = limit_wait
        self.attempts = attempts
        self.clock = clock
        self._next = 0.0

    def request(self, method: str, url: str, *, what: str, json=None):
        for attempt in range(1, self.attempts + 1):
            wait = self._next - self.clock()
            if wait > 0:
                self.http.sleep(wait)
            self._next = max(self.clock(), self._next) + self.interval
            try:
                return self.http.request(method, url, what=what, json=json, attempts=1)
            except HttpError as exc:
                if attempt == self.attempts or (exc.status is not None and exc.status != 429 and exc.status < 500):
                    raise
                if exc.status == 429:
                    self._next = self.clock() + self.limit_wait
        raise AssertionError("not reached")


def _detail(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    message = None
    if isinstance(body, dict):
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        message = message or body.get("description") or body.get("message")
    return f" ({clean_text(message, 200)})" if message else ""


def _retry_after(resp) -> float | None:
    try:
        return float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


class SolanaRPC:
    """Read-only JSON-RPC calls. Nothing here can sign or send a transaction."""

    def __init__(self, url: str, http: HttpClient, *, commitment="confirmed", public=False):
        self.url = url
        self.http = http
        self.commitment = commitment
        self.public = public
        self.tx_version = 1  # highest transaction version we accept; raised if the node reports newer ones
        self.helius = "helius" in (urlsplit(url).hostname or "")
        self.history_api = None  # getTransactionsForAddress: None = not tried yet, then True / False
        self.max_rps = None      # client-side pace for bulk reads (requests per second), see limit_rate()
        self.credits = 0         # estimated provider credits used (Helius: 1 per standard call)
        self._ids = itertools.count(1)
        self._next_request = 0.0
        self._rps_cap = None
        self._limits_seen = 0
        self._calm = 0

    def limit_rate(self, rps: float | None) -> None:
        """Pace requests at up to `rps` per second (None: no pacing). The pace halves whenever the provider
        answers HTTP 429 and creeps back up after 50 requests without one."""
        self.max_rps = self._rps_cap = rps
        self._limits_seen, self._calm = self.http.rate_limited, 0

    def _throttle(self) -> None:
        if not self.max_rps:
            return
        if self.http.rate_limited > self._limits_seen:
            self._limits_seen, self._calm = self.http.rate_limited, 0
            self.max_rps = max(0.1, self.max_rps / 2)
            log.info("the RPC is rate-limiting; slowing down to %.2f requests/s", self.max_rps)
        else:
            self._calm += 1
            if self._calm >= 50 and self.max_rps < self._rps_cap:
                self.max_rps, self._calm = min(self._rps_cap, self.max_rps * 1.25), 0
        wait = self._next_request - time.monotonic()
        if wait > 0:
            self.http.sleep(wait)
        self._next_request = max(time.monotonic(), self._next_request) + 1 / self.max_rps

    def call(self, method: str, params: list):
        what = f"RPC {method}"
        for attempt in range(1, self.http.attempts + 1):
            self._throttle()
            self.credits += 1
            payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
            try:
                resp = self.http.request("POST", self.url, what=what, json=payload)
            except HttpError as exc:
                if exc.status == 429 and self.public:
                    raise HttpError(what, f"{exc.message}. {PUBLIC_RPC_HINT}", 429) from None
                raise
            try:
                body = resp.json()
            except ValueError:
                raise RpcError(method, None, "response was not JSON") from None
            error = body.get("error") if isinstance(body, dict) else None
            if not error:
                return body.get("result") if isinstance(body, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            message = clean_text(error.get("message", error) if isinstance(error, dict) else error, 300)
            if code in RETRY_RPC_CODES and attempt < self.http.attempts:
                self.http.sleep(self.http.backoff(attempt))
                continue
            if code == 429 and self.public:
                message = f"{message}. {PUBLIC_RPC_HINT}"
            raise RpcError(method, code, message)
        raise RpcError(method, None, "retries exhausted")  # not reached: the loop returns or raises

    def token_supply(self, mint: str) -> tuple[int, int]:
        value = self.call("getTokenSupply", [mint, {"commitment": self.commitment}])["value"]
        return int(value["amount"]), int(value["decimals"])

    def largest_token_accounts(self, mint: str) -> list[dict]:
        return self.call("getTokenLargestAccounts", [mint, {"commitment": self.commitment}])["value"]

    def multiple_accounts(self, addresses: list[str], *, encoding="jsonParsed", data_slice=None) -> list:
        accounts = []
        for start in range(0, len(addresses), 100):  # RPC limit: 100 accounts per call
            chunk = addresses[start:start + 100]
            options = {"encoding": encoding, "commitment": self.commitment}
            if data_slice:
                options["dataSlice"] = data_slice
            values = self.call("getMultipleAccounts", [chunk, options])["value"]
            if len(values) != len(chunk):
                raise RpcError("getMultipleAccounts", None, f"asked for {len(chunk)} accounts, got {len(values)}")
            accounts.extend(values)
        return accounts

    def token_accounts_by_owner(self, owner: str, mint: str) -> list[dict]:
        return self.call("getTokenAccountsByOwner",
                         [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": self.commitment}])["value"]

    def account_info(self, address: str) -> dict | None:
        return self.call("getAccountInfo", [address, {"encoding": "jsonParsed", "commitment": self.commitment}])["value"]

    def signatures(self, address: str, limit: int = 10, *, before: str | None = None,
                   until: str | None = None) -> list[dict]:
        """Newest first. `before` pages backwards; `until` stops at (and excludes) a known signature."""
        options = {"limit": limit, "commitment": self.commitment}
        if before:
            options["before"] = before
        if until:
            options["until"] = until
        return self.call("getSignaturesForAddress", [address, options]) or []

    def address_transactions(self, address: str, *, limit: int, since_ts: float | None = None,
                             cache: dict | None = None, progress=None, all_or_none: bool = False) -> tuple[list, bool]:
        """Successful transactions that reference `address`, newest first, as (signature, transaction)
        pairs: at most `limit`, none older than `since_ts`. Also returns whether that was all of them.

        Uses Helius' getTransactionsForAddress when the provider offers it (up to 1000 transactions per
        call for about a tenth of the credits), otherwise getSignaturesForAddress + getTransaction.
        `cache` (signature -> transaction) is filled and reused, so nothing is fetched twice.
        progress(done, total) is called while transactions are fetched one by one. all_or_none: when
        there are more than `limit`, return none (and fetch none one by one).
        """
        cache = {} if cache is None else cache
        if self.helius and self.history_api is not False:
            try:
                result = self._history_bulk(address, limit, since_ts, cache)
            except (HttpError, RpcError) as exc:
                if self.history_api:
                    raise
                log.info("getTransactionsForAddress is not available (%s); reading history with "
                         "getSignaturesForAddress + getTransaction", exc)
                self.history_api = False
            else:
                self.history_api = True
                return result if result[1] or not all_or_none else ([], False)
        return self._history_standard(address, limit, since_ts, cache, progress, all_or_none)

    def _history_bulk(self, address, limit, since_ts, cache):
        items, token = [], None
        while True:
            filters = {"status": "succeeded"}
            if since_ts is not None:
                filters["blockTime"] = {"gte": int(since_ts)}
            options = {"transactionDetails": "full", "encoding": "jsonParsed", "sortOrder": "desc",
                       "maxSupportedTransactionVersion": self.tx_version, "commitment": self.commitment,
                       "limit": min(1000, limit + 1 - len(items)), "filters": filters}
            if token:
                options["paginationToken"] = token
            result = self.call("getTransactionsForAddress", [address, options])
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                raise RpcError("getTransactionsForAddress", None, "unexpected response")
            self.credits += max(10, 10 * math.ceil(len(data) / 100)) - 1  # metered per 100 transactions
            for tx in data:
                sig = tx_signature(tx) if isinstance(tx, dict) else None
                if sig:
                    items.append((sig, cache.setdefault(sig, tx)))
            token = result.get("paginationToken")
            if len(items) > limit:
                return items[:limit], False
            if not token or not data:
                return items, True

    def _history_standard(self, address, limit, since_ts, cache, progress=None, all_or_none=False):
        found, before = [], None
        while True:
            page = self.signatures(address, limit=1000, before=before)
            done = len(page) < 1000
            for item in page:
                if since_ts is not None and item.get("blockTime") is not None and item["blockTime"] < since_ts:
                    done = True
                    break
                if item.get("err") is None:
                    found.append(item["signature"])
            if len(found) > limit:
                if all_or_none:  # the signature list alone settles it: don't fetch transactions in vain
                    return [], False
                found, complete = found[:limit], False
                break
            if done:
                complete = True
                break
            before = page[-1]["signature"]
        items = []
        for done, sig in enumerate(found, 1):
            if sig not in cache:
                tx = self.transaction(sig)
                if not tx:
                    continue
                cache[sig] = tx
            items.append((sig, cache[sig]))
            if progress and done % 25 == 0:
                progress(done, len(found))
        return items, complete

    def transaction(self, signature: str) -> dict | None:
        while True:
            options = {"encoding": "jsonParsed", "commitment": self.commitment,
                       "maxSupportedTransactionVersion": self.tx_version}
            try:
                return self.call("getTransaction", [signature, options])
            except RpcError as exc:
                newer = re.search(r"version \((\d+)\)", exc.message or "")
                if exc.code == -32015 and newer and int(newer.group(1)) > self.tx_version:
                    self.tx_version = int(newer.group(1))
                    continue
                raise
