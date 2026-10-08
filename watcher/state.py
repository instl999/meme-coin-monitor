"""state.json and trader_state.json: what the monitors must remember across restarts, saved atomically."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("holder_watch")

STATE_VERSION = 1
TRADER_STATE_VERSION = 1


def new_state(mint: str) -> dict:
    return {
        "version": STATE_VERSION,
        "mint": mint,
        "accounts": {},      # watched token account -> owner (kept after it leaves the top 20)
        "owners": {},        # owner -> {"first_seen", "history": [[ts, raw amount], ...], "always_ref", ...}
        "owner_kinds": {},   # pool-detection cache: address -> {"pool": name or None, "program": id}
        "top_owners": [],
        "peak": None,        # {"usd", "ts"}: highest price seen, for the trailing stop
        "alerts": {},        # alert key -> when it was last delivered (cooldowns and baselines)
        "pending": {},       # alert key -> start of a drop held back by cooldown
        "heartbeat": {"last_ts": None},
        "failures": {"count": 0, "since": None, "alerted": False, "last_errors": []},
        "stats": {"cycles": 0, "failed": 0, "alerts": 0},
        "last": {},          # latest market / holder summary for heartbeats and logs
    }


def new_trader_state() -> dict:
    return {
        "version": TRADER_STATE_VERSION,
        "wallets": {},       # "chain:address" -> where the next check starts. Solana: {"last_signature",
                             # "since"}; EVM: {"nonce", "block", "since", "done": blocks read, "active_*"}
        "owner_kinds": {},   # pool-detection cache, as in state.json
        "alerted": [],       # times of trade alerts in the last 24 h, for the heartbeat
        "alerts": {},        # alert key -> when it was last delivered
        "failures": {"count": 0, "since": None, "alerted": False, "last_errors": []},
        "last_check": None,
        "heartbeat_ts": None,  # traders-only runs: when the last daily heartbeat went out
    }


def atomic_write(path, text: str, *, mode: int | None = None) -> None:
    """Write to a temp file, fsync, then atomically replace: a crash never leaves a half-written file."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None and os.name == "posix":
        os.chmod(tmp, mode)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:  # Windows: file briefly locked by antivirus or an indexer
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class StateStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self, mint: str) -> dict:
        data = self._read(STATE_VERSION)
        if data is None:
            return new_state(mint)
        if data.get("mint") != mint:
            backup = self._set_aside("other-mint")
            log.warning("%s belongs to mint %s, not %s; moved it to %s and starting fresh",
                        self.path, data.get("mint"), mint, backup.name)
            return new_state(mint)
        for key, value in new_state(mint).items():  # fields added in later versions
            data.setdefault(key, value)
        return data

    def save(self, state: dict) -> None:
        atomic_write(self.path, json.dumps(state, indent=1))

    def _read(self, version: int) -> dict | None:
        """The saved state, or None if there is none or it can't be trusted (then it is set aside)."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != version:
                raise ValueError("unrecognised format")
        except (OSError, ValueError) as exc:
            backup = self._set_aside("corrupt")
            log.error("could not read %s (%s); moved it to %s and starting fresh", self.path, exc, backup.name)
            return None
        return data

    def _set_aside(self, reason: str) -> Path:
        backup = self.path.with_name(f"{self.path.name}.{reason}-{int(time.time())}")
        os.replace(self.path, backup)
        return backup


class TraderStateStore(StateStore):
    def load(self) -> dict:  # one file for every watched trader, whatever token the holder monitor follows
        data = self._read(TRADER_STATE_VERSION)
        if data is None:
            return new_trader_state()
        for key, value in new_trader_state().items():
            data.setdefault(key, value)
        return data
