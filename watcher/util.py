"""Small shared helpers: Solana address checks, number/time formatting, text cleanup, redaction."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {ch: i for i, ch in enumerate(_B58_ALPHABET)}


def b58decode(text: str) -> bytes:
    num = 0
    for ch in text:
        if ch not in _B58_INDEX:
            raise ValueError(f"invalid base58 character {ch!r}")
        num = num * 58 + _B58_INDEX[ch]
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def is_pubkey(value: object) -> bool:
    """True if value is a base58 string that decodes to 32 bytes, i.e. a Solana address."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return False
    try:
        return len(b58decode(value)) == 32
    except ValueError:
        return False


def short(address: str | None) -> str:
    if not address:
        return "?"
    if address.startswith("0x") and len(address) == 42:  # EVM: past the 0x prefix, as explorers show it
        return f"{address[:6]}…{address[-4:]}"
    return address if len(address) <= 10 else f"{address[:4]}…{address[-4:]}"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "n/a"
    magnitude = abs(value)
    if magnitude == 0:
        return "$0"
    if magnitude >= 1000:
        return f"${value:,.0f}"
    if magnitude >= 1:
        return f"${value:,.2f}"
    decimals = min(12, 3 - math.floor(math.log10(magnitude)))  # 4 significant digits
    return f"${value:.{decimals}f}"


def fmt_number(value: float) -> str:
    magnitude = abs(value)
    if magnitude == 0 or magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 1:
        return f"{value:,.2f}"
    if magnitude < 0.001:  # meme-coin prices in SOL: fixed notation, 4 significant digits
        return f"{value:.{min(12, 3 - math.floor(math.log10(magnitude)))}f}"
    return f"{value:.4g}"


def fmt_tokens(raw: int, decimals: int) -> str:
    """Raw on-chain amount -> human amount, e.g. 1459668419511158 (9 decimals) -> '1,459,668'."""
    return fmt_number(raw / 10**decimals)


def drop_pct(before: float, after: float) -> float:
    """How far `after` is below `before`, in percent (positive = decrease)."""
    return (before - after) / before * 100 if before else 0.0


def utc(ts: float, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(fmt)


def iso_time(value) -> float | None:
    """Unix time from an ISO-8601 string such as '2026-10-07T10:37:41Z', or None."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fmt_age(seconds: float) -> str:
    """A duration such as '45 min', '7 h' or '12 d'."""
    if seconds < 3600:
        return f"{max(0, seconds) / 60:.0f} min"
    if seconds < 2 * 86_400:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86_400:.0f} d"


# Control and bidirectional-override characters. Look-alike scam tokens use U+202E etc. to make a
# name render as something else, so never show external text without removing them.
_UNSAFE_CHARS = re.compile("[\x00-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")


def clean_text(value: object, limit: int = 64) -> str:
    return _UNSAFE_CHARS.sub("", str(value or "")).strip()[:limit]


def has_hidden_chars(value: object) -> bool:
    return bool(_UNSAFE_CHARS.search(str(value or "")))


def solscan_tx(signature: str) -> str:
    return f"https://solscan.io/tx/{signature}"


def solscan_account(address: str) -> str:
    return f"https://solscan.io/account/{address}"


def solscan_token(mint: str) -> str:
    return f"https://solscan.io/token/{mint}"


class Redactor:
    """Masks secrets (RPC API keys, Telegram bot token) in text before it is logged or stored."""

    _PATTERNS = (
        (re.compile(r"(?i)(api[-_]?key=)[^&\s\"'<>]+"), r"\1***"),
        (re.compile(r"bot\d{3,}:[A-Za-z0-9_-]{10,}"), "bot***"),
    )

    def __init__(self, secrets=()):
        self.secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: object) -> str:
        text = str(text)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        for pattern, replacement in self._PATTERNS:
            text = pattern.sub(replacement, text)
        return text
