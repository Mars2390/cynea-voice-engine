"""Cynea Africa — Operations dashboard metrics.

Tracks per-call metrics and aggregates them across calls. The two outputs
ops cares about most are:

  - Cost per call (broken down by STT / LLM / TTS / telephony)
  - Containment rate (calls resolved without human handoff)

Both are exported to JSON or CSV with no external dependencies.

Design choices
--------------
- Sentiment is a tiny lexicon scorer, NOT a model. We're targeting 8 GB RAM
  and zero API spend by default; loading a transformer for sentiment is
  not in scope. The lexicon is good enough for trend lines and red-flag
  detection. Customers who want better sentiment can plug in their own
  scorer via `set_sentiment_scorer(callable)`.

- Costs are configured via a `RateCard` (cents per second / per token /
  per character / per minute), not hardcoded. Default rates are placeholder
  values for the African market — replace with your contracted rates.

- All times are seconds (float, monotonic). All costs are USD cents (float)
  to avoid floating-point dollar drift.

Usage
-----
    tracker = MetricsTracker(rate_card=RateCard.default_africa())
    metrics = tracker.start_call(call_id="abc-123", agent="kwame")

    metrics.record_user_turn("Hi, do you have rooms?")
    metrics.record_assistant_turn(
        text="Yes, what dates?", llm_input_tokens=80, llm_output_tokens=8,
        tts_chars=18,
    )
    metrics.record_stt_duration(2.4)
    metrics.record_telephony_seconds(3.1)
    metrics.record_interruption()
    metrics.set_outcome(containment=True, resolution=True)

    tracker.end_call(metrics)
    print(tracker.summary())
    tracker.export_json("calls.json")
    tracker.export_csv("calls.csv")
"""

from __future__ import annotations

import csv
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional


# ----------------------------------------------------------------------
# Sentiment lexicon (tiny, heuristic — replace with an LLM if you have budget)
# ----------------------------------------------------------------------

_POS_WORDS = frozenset({
    "thanks", "thank", "great", "good", "perfect", "lovely", "amazing",
    "wonderful", "excellent", "asante", "tafadhali", "poa", "sawa",
    "fine", "happy", "love", "yes", "please",
})
_NEG_WORDS = frozenset({
    "no", "bad", "terrible", "awful", "horrible", "rude", "wrong",
    "stupid", "useless", "frustrated", "angry", "annoyed", "complaint",
    "refund", "cancel", "shauri", "haitoshi",
})


def _default_sentiment(text: str) -> float:
    """Return a sentiment score in [-1, 1]. 0 = neutral.

    English + a few Swahili tokens. Good enough for trends, not for
    individual-call decisions.
    """
    if not text:
        return 0.0
    tokens = text.lower().split()
    pos = sum(1 for t in tokens if t.strip(".,!?") in _POS_WORDS)
    neg = sum(1 for t in tokens if t.strip(".,!?") in _NEG_WORDS)
    if pos == 0 and neg == 0:
        return 0.0
    return (pos - neg) / max(1, pos + neg)


# ----------------------------------------------------------------------
# Rate card
# ----------------------------------------------------------------------

@dataclass
class RateCard:
    """Pricing for one provider stack. All units are USD cents.

    Defaults are placeholder values reflecting roughly:
    - Whisper-base on local CPU: 0 cents (free, just compute)
    - Edge TTS: 0 cents (free)
    - Anthropic Sonnet input: 0.3 c/1k input tok, 1.5 c/1k output tok
    - Africa's Talking voice: ~5 c/min outbound in Kenya
    Override these when you have your real contracts.
    """

    stt_cents_per_second: float = 0.0          # Whisper local = free
    llm_cents_per_input_kilotoken: float = 0.3
    llm_cents_per_output_kilotoken: float = 1.5
    tts_cents_per_kilochar: float = 0.0        # Edge TTS = free
    telephony_cents_per_minute: float = 5.0    # AT Kenya outbound, approx

    @classmethod
    def default_africa(cls) -> "RateCard":
        return cls()

    @classmethod
    def free_stack(cls) -> "RateCard":
        """All-zero rate card for local development."""
        return cls(
            stt_cents_per_second=0.0,
            llm_cents_per_input_kilotoken=0.0,
            llm_cents_per_output_kilotoken=0.0,
            tts_cents_per_kilochar=0.0,
            telephony_cents_per_minute=0.0,
        )


# ----------------------------------------------------------------------
# Per-call metrics
# ----------------------------------------------------------------------

@dataclass
class CallRecord:
    """Everything we know about a single call. Serializable."""

    call_id: str
    agent: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None

    # Counters
    user_turns: int = 0
    assistant_turns: int = 0
    interruptions: int = 0

    # Provider usage
    stt_seconds: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_characters: int = 0
    telephony_seconds: float = 0.0

    # Sentiment running average over user turns
    sentiment_score: float = 0.0
    _sentiment_sum: float = 0.0
    _sentiment_n: int = 0

    # Outcomes (set by the application, not derivable)
    containment: Optional[bool] = None       # True = resolved without human handoff
    resolution: Optional[bool] = None        # True = caller's intent satisfied
    handoff_reason: Optional[str] = None
    notes: Optional[str] = None

    # Computed at end_call()
    duration_s: Optional[float] = None
    cost_breakdown: dict = field(default_factory=dict)
    cost_total_cents: float = 0.0

    # ------------------------------------------------------------------
    # Recording API
    # ------------------------------------------------------------------

    def record_user_turn(self, text: str, *, sentiment_scorer: Callable[[str], float] = _default_sentiment) -> None:
        self.user_turns += 1
        score = sentiment_scorer(text or "")
        self._sentiment_sum += score
        self._sentiment_n += 1
        self.sentiment_score = self._sentiment_sum / self._sentiment_n

    def record_assistant_turn(
        self,
        *,
        text: str = "",
        llm_input_tokens: int = 0,
        llm_output_tokens: int = 0,
        tts_chars: Optional[int] = None,
    ) -> None:
        self.assistant_turns += 1
        self.llm_input_tokens += max(0, int(llm_input_tokens))
        self.llm_output_tokens += max(0, int(llm_output_tokens))
        self.tts_characters += int(tts_chars if tts_chars is not None else len(text or ""))

    def record_stt_duration(self, seconds: float) -> None:
        self.stt_seconds += max(0.0, float(seconds))

    def record_telephony_seconds(self, seconds: float) -> None:
        self.telephony_seconds += max(0.0, float(seconds))

    def record_interruption(self) -> None:
        self.interruptions += 1

    def set_outcome(
        self,
        *,
        containment: Optional[bool] = None,
        resolution: Optional[bool] = None,
        handoff_reason: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        if containment is not None:
            self.containment = containment
        if resolution is not None:
            self.resolution = resolution
        if handoff_reason is not None:
            self.handoff_reason = handoff_reason
        if notes is not None:
            self.notes = notes

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def finalize(self, rate_card: RateCard) -> None:
        """Compute duration + cost. Idempotent."""
        if self.ended_at is None:
            self.ended_at = time.time()
        self.duration_s = round(self.ended_at - self.started_at, 3)

        stt_cost = self.stt_seconds * rate_card.stt_cents_per_second
        llm_cost = (
            (self.llm_input_tokens / 1000.0) * rate_card.llm_cents_per_input_kilotoken
            + (self.llm_output_tokens / 1000.0) * rate_card.llm_cents_per_output_kilotoken
        )
        tts_cost = (self.tts_characters / 1000.0) * rate_card.tts_cents_per_kilochar
        tel_cost = (self.telephony_seconds / 60.0) * rate_card.telephony_cents_per_minute

        self.cost_breakdown = {
            "stt_cents": round(stt_cost, 4),
            "llm_cents": round(llm_cost, 4),
            "tts_cents": round(tts_cost, 4),
            "telephony_cents": round(tel_cost, 4),
        }
        self.cost_total_cents = round(stt_cost + llm_cost + tts_cost + tel_cost, 4)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Strip the running sums from the public view — they're an
        # implementation detail of the running average.
        d.pop("_sentiment_sum", None)
        d.pop("_sentiment_n", None)
        return d


# ----------------------------------------------------------------------
# Tracker — owns the rate card and aggregate stats
# ----------------------------------------------------------------------

class MetricsTracker:
    """Aggregates many CallRecords. Supports JSON and CSV export."""

    def __init__(
        self,
        rate_card: Optional[RateCard] = None,
        sentiment_scorer: Optional[Callable[[str], float]] = None,
    ):
        self.rate_card = rate_card or RateCard.default_africa()
        self.sentiment_scorer = sentiment_scorer or _default_sentiment
        self.calls: list = []

    def set_sentiment_scorer(self, scorer: Callable[[str], float]) -> None:
        """Plug in a stronger sentiment model if you have one."""
        self.sentiment_scorer = scorer

    def start_call(self, *, call_id: Optional[str] = None, agent: str = "unknown") -> CallRecord:
        rec = CallRecord(call_id=call_id or str(uuid.uuid4()), agent=agent)
        return rec

    def end_call(self, record: CallRecord) -> CallRecord:
        record.finalize(self.rate_card)
        self.calls.append(record)
        return record

    # ------------------------------------------------------------------
    # Aggregate views
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Headline numbers for an ops dashboard."""
        if not self.calls:
            return {
                "calls": 0,
                "containment_rate": None,
                "resolution_rate": None,
                "avg_duration_s": None,
                "avg_cost_cents": None,
                "total_cost_cents": 0.0,
                "avg_sentiment": None,
                "interruptions_per_call": None,
            }

        n = len(self.calls)
        contained = sum(1 for c in self.calls if c.containment is True)
        resolved = sum(1 for c in self.calls if c.resolution is True)
        contain_known = sum(1 for c in self.calls if c.containment is not None)
        resolve_known = sum(1 for c in self.calls if c.resolution is not None)
        return {
            "calls": n,
            "containment_rate": round(contained / contain_known, 3) if contain_known else None,
            "resolution_rate": round(resolved / resolve_known, 3) if resolve_known else None,
            "avg_duration_s": round(sum((c.duration_s or 0) for c in self.calls) / n, 2),
            "avg_cost_cents": round(sum(c.cost_total_cents for c in self.calls) / n, 4),
            "total_cost_cents": round(sum(c.cost_total_cents for c in self.calls), 4),
            "avg_sentiment": round(sum(c.sentiment_score for c in self.calls) / n, 3),
            "interruptions_per_call": round(sum(c.interruptions for c in self.calls) / n, 2),
        }

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_json(self, path: str) -> None:
        payload = {
            "summary": self.summary(),
            "rate_card": asdict(self.rate_card),
            "calls": [c.to_dict() for c in self.calls],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)

    def export_csv(self, path: str) -> None:
        """Flat per-call CSV with cost breakdown columns inlined."""
        if not self.calls:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("")
            return

        fieldnames = [
            "call_id", "agent", "started_at", "ended_at", "duration_s",
            "user_turns", "assistant_turns", "interruptions",
            "stt_seconds", "llm_input_tokens", "llm_output_tokens",
            "tts_characters", "telephony_seconds",
            "sentiment_score", "containment", "resolution", "handoff_reason",
            "stt_cents", "llm_cents", "tts_cents", "telephony_cents",
            "cost_total_cents",
        ]
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for c in self.calls:
                d = c.to_dict()
                row = {k: d.get(k) for k in fieldnames if k in d}
                row.update(c.cost_breakdown)
                row["cost_total_cents"] = c.cost_total_cents
                writer.writerow(row)
