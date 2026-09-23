"""Pure event classifier, not a deployed controller. No I/O or motion commands.

Feed distinct stream messages with monotonic reception times; not cached polls.
Reception time does not prove camera exposure freshness. Call tick on silence.
"""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str
    distance_m: float | None


class SentryRules:
    def __init__(self):
        self.last_received = None
        self.last_alert = -math.inf
        self.latched = False
        self.candidate = None
        self.count = 0
        self.since = None

    def _reset(self):
        self.candidate, self.count, self.since = None, 0, None

    def tick(self, now):
        if self.last_received is not None and now - self.last_received > 2:
            self._reset()
            return Event('unavailable', None)
        return None

    def feed(self, sample, *, received_at, now):
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (received_at, now)):
            self._reset()
            return Event('unavailable', None)
        if received_at > now or now - received_at > 2:
            self._reset()
            return Event('unavailable', None)
        if self.last_received is not None and received_at <= self.last_received:
            return None
        if self.last_received is not None and received_at - self.last_received > 2:
            self._reset()
        self.last_received = received_at
        d = sample.get('distance_m') if isinstance(sample, dict) else None
        if isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d <= 0:
            self._reset()
            return Event('unavailable', None)
        kind = 'occlusion_suspected' if d < .15 else 'near' if d < .8 else 'clear' if d > 1 else 'hold'
        if kind == 'hold':
            self._reset()
            return None
        if self.candidate != kind:
            self.candidate, self.count, self.since = kind, 1, received_at
        else:
            self.count += 1
        if self.count < 3 or received_at - self.since < .2 - 1e-9:
            return None
        if kind == 'clear':
            was_latched = self.latched
            self.latched = False
            return Event('clear', d) if was_latched else None
        if not self.latched and now - self.last_alert >= 5:
            self.latched = True
            self.last_alert = now
            return Event(kind, d)
        return None
