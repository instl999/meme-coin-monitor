"""Change-only balance history: a time-sorted list of [unix_ts, raw_amount] entries per owner.

An entry is added only when the balance changes, so the balance at any moment is the latest
entry at or before it.
"""

from __future__ import annotations


def record(history: list, ts: float, amount: int) -> bool:
    if history and history[-1][1] == amount:
        return False
    history.append([ts, amount])
    return True


def value_at(history: list, ts: float) -> int | None:
    value = None
    for entry_ts, amount in history:
        if entry_ts > ts:
            break
        value = amount
    return value


def peak_since(history: list, since: float) -> tuple[int, float] | None:
    """Highest balance from `since` until now, and when it was at that level."""
    start = value_at(history, since)
    best = (start, since) if start is not None else None
    for entry_ts, amount in history:
        if entry_ts > since and (best is None or amount > best[0]):
            best = (amount, entry_ts)
    return best


def first_drop_after(history: list, ts: float, level: int) -> float | None:
    """When the balance was first seen below `level` after `ts`."""
    for entry_ts, amount in history:
        if entry_ts > ts and amount < level:
            return entry_ts
    return None


def prune(history: list, cutoff: float) -> None:
    """Drop entries older than cutoff, keeping the one that still defines the balance at cutoff."""
    keep_from = 0
    for i, (entry_ts, _) in enumerate(history):
        if entry_ts > cutoff:
            break
        keep_from = i
    del history[:keep_from]
