"""Счётчики процесса без параметров, идентификаторов и содержимого запросов."""
from collections import Counter
from threading import Lock


class Metrics:
    allowed = frozenset({"http", "http_denied", "http_error", "throttled", "delivery_confirmed",
                         "delivery_failed", "delivery_delivered", "delivery_partially_delivered", "delivery_cancelled", "delivery_expired", "delivery_unknown"})
    def __init__(self):
        self.counts = Counter()
        self.lock = Lock()

    def add(self, name, count=1):
        if name in self.allowed:
            with self.lock:
                self.counts[name] += count

    def snapshot(self):
        with self.lock:
            return {name: self.counts[name] for name in sorted(self.allowed)}
