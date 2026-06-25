"""Guards the capture-hook grounding fix (2026-06-25): injected channel/relay
content must not produce false-positive learning candidates, and the dropped
conversational phrases must no longer fire."""
import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "capture_learning_candidate.py"
spec = importlib.util.spec_from_file_location("capture_hook", _SRC)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
PHRASES, SKIPS = hook._load_triggers()


def _fires(prompt):
    return hook._scan(hook._strip_injected(prompt), PHRASES, SKIPS)


def test_relay_injected_trigger_is_suppressed():
    # a sibling's relayed prose (or our own) mentioning a trigger word is NOT
    # an operator correction of this session.
    relay = '<channel source="treadmill-events">[from: treadmill-alan] the number was fabricated</channel>'
    assert _fires(relay) is None


def test_system_reminder_block_is_stripped():
    assert _fires("<system-reminder>you're wrong about the path</system-reminder>") is None


def test_genuine_operator_correction_still_fires():
    assert _fires("No — you fabricated that, back it out") is not None


def test_dropped_conversational_phrases_do_not_fire():
    for p in ("hold on a sec", "i don't think that's it", "wait, let me check", "actually, never mind"):
        assert _fires(p) is None, p


def test_strong_corrections_retained():
    assert _fires("that's wrong") == "that's wrong"
    assert _fires("you hallucinated the API") == "you hallucinated"
