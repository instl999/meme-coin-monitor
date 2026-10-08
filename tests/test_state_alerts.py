"""state.json persistence, message formatting/delivery, log redaction, --list and --test-alert."""

import io
import json
import logging
import os

import holder_watch
from tests.fakes import CPMM_AUTHORITY, MINT, TELEGRAM_TOKEN, Harness, addr, plain, populate
from watcher.alerts import Bold, Link, Notifier, RedactingFormatter, render_html, render_plain, split_message
from watcher.listing import list_holders
from watcher.state import StateStore, new_state
from watcher.util import Redactor, fmt_usd, is_pubkey


def test_state_round_trips_big_amounts_and_leaves_no_temp_files(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = new_state(MINT)
    state["owners"]["w"] = {"first_seen": 1.0, "history": [[1.0, 729_894_775_733_500_753]]}  # > 2**53
    store.save(state)
    store.save(state)
    assert store.load(MINT) == state
    assert os.listdir(tmp_path) == ["state.json"]


def test_unreadable_state_is_set_aside_not_trusted(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ half-written", encoding="utf-8")
    state = StateStore(path).load(MINT)
    assert state == new_state(MINT)
    assert [p.name.startswith("state.json.corrupt-") for p in tmp_path.iterdir()] == [True]


def test_state_for_another_mint_is_not_reused(tmp_path):
    store = StateStore(tmp_path / "state.json")
    other = new_state(addr("other-mint"))
    other["peak"] = {"usd": 99.0, "ts": 1.0}
    store.save(other)
    assert store.load(MINT)["peak"] is None
    assert any(p.name.startswith("state.json.other-mint-") for p in tmp_path.iterdir())


def test_long_messages_split_on_line_boundaries():
    text = "\n".join(f"line {i:03d} " + "x" * 50 for i in range(200))
    chunks = split_message(text, 4000)
    assert len(chunks) > 1 and all(len(chunk) <= 4000 for chunk in chunks)
    assert "\n".join(chunks) == text


def test_html_escapes_external_text_but_keeps_links():
    lines = [[Bold("A&B <monitor>"), " label <script>x</script> ", Link("tx", "https://solscan.io/tx/a?b=1&c=2")]]
    assert render_html(lines) == ('<b>A&amp;B &lt;monitor&gt;</b> label &lt;script&gt;x&lt;/script&gt; '
                                  '<a href="https://solscan.io/tx/a?b=1&amp;c=2">tx</a>')
    assert render_plain(lines) == "A&B <monitor> label <script>x</script> tx (https://solscan.io/tx/a?b=1&c=2)"


def test_log_formatter_masks_secrets_even_in_tracebacks():
    formatter = RedactingFormatter(Redactor(["my-helius-key-123"]))
    try:
        raise RuntimeError("GET https://mainnet.helius-rpc.com/?api-key=my-helius-key-123 failed")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "token bot%s/sendMessage",
                                   (TELEGRAM_TOKEN,), exc_info=True)
        import sys
        record.exc_info = sys.exc_info()
    text = formatter.format(record)
    assert "my-helius-key-123" not in text and TELEGRAM_TOKEN not in text
    assert "bot***" in text and "api-key=***" in text


def test_notifier_without_telegram_logs_and_reports_delivered(caplog):
    with caplog.at_level(logging.INFO):
        assert Notifier(None).send([["hello"]], level=logging.INFO)
    assert "hello" in caplog.text


def test_list_shows_balance_share_label_and_excluded_flag(tmp_path):
    lp = addr("lp-wallet")
    h = Harness(tmp_path, exclude_owners=[lp], labels={addr("whale1"): "Whale One"})
    populate(h.chain)
    h.chain.hold(lp, 100_000_000)
    out = io.StringIO()
    assert list_holders(h.monitor.cfg, h.monitor.rpc, h.monitor.http, out=out) == 0
    text = out.getvalue()
    rows = {line.split()[1]: line for line in text.splitlines() if line[:3].strip().isdigit()}
    assert "yes (auto: Raydium CPMM pool)" in rows[CPMM_AUTHORITY]
    assert "yes (exclude_owners)" in rows[lp]
    whale = rows[addr("whale1")]
    assert "50,000,000" in whale and "5.00%" in whale and "Whale One" in whale and " top " in whale
    assert "Watched: the top 5 non-excluded owners hold 15.00% of supply" in text
    assert "Program SPL Token · decimals 6 · mint authority: none (supply is fixed)" in text
    assert not (tmp_path / "state.json").exists()  # --list is read-only


def test_test_alert_goes_to_telegram(tmp_path, capsys):
    h = Harness(tmp_path)
    assert holder_watch.send_test_alert(h.monitor.cfg, h.monitor.notifier) == 0
    [message] = h.take()
    assert "test alert" in message and "no rule fired" in plain(message)
    assert "Test alert sent to Telegram." in capsys.readouterr().out


def test_main_reports_config_problems_and_exits_2(tmp_path, capsys):
    (tmp_path / "config.json").write_text(json.dumps({"mint": "PASTE_MINT_HERE"}), encoding="utf-8")
    assert holder_watch.main(["--config", str(tmp_path / "config.json"), "--once"]) == 2
    assert "replace 'PASTE_MINT_HERE'" in capsys.readouterr().err


def test_helpers():
    assert is_pubkey(MINT) and is_pubkey("1nc1nerator11111111111111111111111111111111")
    assert not is_pubkey("0OIl" * 10) and not is_pubkey("short")
    assert fmt_usd(0.001710) == "$0.001710" and fmt_usd(695_267.38) == "$695,267" and fmt_usd(1.5) == "$1.50"
