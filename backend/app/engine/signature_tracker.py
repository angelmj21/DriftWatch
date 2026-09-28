"""Track new and shifting error signatures in rolling event windows."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime

from ..config import settings


@dataclass
class SigCount:
    signature: str
    template: str
    count: int


@dataclass
class SigFinding:
    new_signatures: list[SigCount]
    shifted: list[SigCount]


@dataclass
class _ObservedSignature:
    ts: datetime
    signature: str
    template: str
    service: str


class SignatureTracker:
    def __init__(self) -> None:
        self._short_events: deque[_ObservedSignature] = deque()
        self._long_events: deque[_ObservedSignature] = deque()
        self._short_counts: Counter[str] = Counter()
        self._long_counts: Counter[str] = Counter()
        self._short_total = 0
        self._long_total = 0
        self._templates: dict[str, str] = {}
        self._known_signatures: set[str] = set()
        self._started_at: datetime | None = None
        self._latest_ts: datetime | None = None

    def observe(self, ts: datetime, signature: str, template: str, service: str) -> None:
        if self._started_at is None:
            self._started_at = ts
        if self._latest_ts is None or ts > self._latest_ts:
            self._latest_ts = ts

        self._prune(self._latest_ts)
        self._templates[signature] = template
        if self._is_learning(ts):
            self._known_signatures.add(signature)

        event = _ObservedSignature(ts, signature, template, service)
        if (self._latest_ts - ts).total_seconds() < settings.window_long_s:
            self._insert_ordered(self._long_events, event)
            self._long_counts[signature] += 1
            self._long_total += 1
        if (self._latest_ts - ts).total_seconds() < settings.window_short_s:
            self._insert_ordered(self._short_events, event)
            self._short_counts[signature] += 1
            self._short_total += 1

    def top(self, n: int = 5) -> list[SigCount]:
        if n <= 0:
            return []
        if self._latest_ts is not None:
            self._prune(self._latest_ts)
        ordered = sorted(self._short_counts.items(), key=lambda item: (-item[1], item[0]))
        return [self._count(signature, count) for signature, count in ordered[:n]]

    def check(self, now: datetime) -> SigFinding:
        if self._started_at is None:
            self._started_at = now
        if self._latest_ts is None or now > self._latest_ts:
            self._latest_ts = now
        self._prune(self._latest_ts)

        if self._is_learning(now):
            self._known_signatures.update(self._long_counts)
            return SigFinding(new_signatures=[], shifted=[])

        new_signatures: list[SigCount] = []
        for signature, count in sorted(self._short_counts.items()):
            if (
                signature not in self._known_signatures
                and count >= settings.sig_min_count
            ):
                new_signatures.append(self._count(signature, count))
                self._known_signatures.add(signature)

        shifted: list[SigCount] = []
        if self._short_total >= settings.sig_min_count and self._long_total > 0:
            for signature in sorted(self._known_signatures - set(s.signature for s in new_signatures)):
                short_count = self._short_counts.get(signature, 0)
                if short_count < settings.sig_min_count:
                    continue
                short_share = short_count / self._short_total
                long_share = self._long_counts.get(signature, 0) / self._long_total
                if short_share >= settings.sig_shift_ratio * long_share:
                    shifted.append(self._count(signature, short_count))

        new_signatures.sort(key=lambda item: (-item.count, item.signature))
        shifted.sort(key=lambda item: (-item.count, item.signature))
        return SigFinding(new_signatures=new_signatures, shifted=shifted)

    def _is_learning(self, ts: datetime) -> bool:
        return (
            self._started_at is not None
            and (ts - self._started_at).total_seconds() < settings.baseline_min_samples
        )

    def _prune(self, now: datetime) -> None:
        short_cutoff = now.timestamp() - settings.window_short_s
        while self._short_events and self._short_events[0].ts.timestamp() <= short_cutoff:
            event = self._short_events.popleft()
            self._decrement(self._short_counts, event.signature)
            self._short_total -= 1

        long_cutoff = now.timestamp() - settings.window_long_s
        while self._long_events and self._long_events[0].ts.timestamp() <= long_cutoff:
            event = self._long_events.popleft()
            self._decrement(self._long_counts, event.signature)
            self._long_total -= 1

    @staticmethod
    def _insert_ordered(
        events: deque[_ObservedSignature], event: _ObservedSignature
    ) -> None:
        if not events or event.ts >= events[-1].ts:
            events.append(event)
            return
        index = len(events)
        while index > 0 and events[index - 1].ts > event.ts:
            index -= 1
        events.insert(index, event)

    @staticmethod
    def _decrement(counts: Counter[str], signature: str) -> None:
        counts[signature] -= 1
        if counts[signature] <= 0:
            del counts[signature]

    def _count(self, signature: str, count: int) -> SigCount:
        return SigCount(
            signature=signature,
            template=self._templates[signature],
            count=count,
        )