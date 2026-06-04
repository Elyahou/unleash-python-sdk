"""Toggle/variant metrics bucket for the embedded engine.

Mirrors the native engine's metrics bucket consumed by
``periodic_tasks/send_metrics.py``. The bucket shape returned by ``snapshot``
is::

    {
        "start": "<UTC ISO8601 Z>",
        "stop":  "<UTC ISO8601 Z>",
        "toggles": {
            "<feature>": {
                "yes": <int>,
                "no": <int>,
                "variants": {"<variant>": <int>, ...},
            },
            ...
        },
    }

``count_toggle`` is called from request threads while ``snapshot`` runs on the
apscheduler thread, so all mutation is guarded by a lock. ``snapshot``
atomically reads-and-resets: it returns the accumulated bucket and starts a
fresh interval. When no activity has been recorded since the last snapshot the
bucket's ``toggles`` is empty; the facade returns a falsy value in that case so
``send_metrics`` skips submission.

This is a self-contained bookkeeping class with no dependency on the rest of
the engine, so it is fully implemented at scaffold time (the facade constructor
relies on ``MetricsBucket()`` not raising).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO8601 string with a trailing ``Z``."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class MetricsBucket:
    """Thread-safe accumulator of toggle and variant evaluation counts."""

    def __init__(self) -> None:
        """Initialize an empty bucket and record the interval start time."""
        self._lock = threading.Lock()
        self._start: str = _utc_now_iso()
        # feature_name -> {"yes": int, "no": int, "variants": {name: int}}
        self._toggles: Dict[str, Dict[str, Any]] = {}

    def _ensure_toggle(self, feature_name: str) -> Dict[str, Any]:
        entry = self._toggles.get(feature_name)
        if entry is None:
            entry = {"yes": 0, "no": 0, "variants": {}}
            self._toggles[feature_name] = entry
        return entry

    def count_toggle(self, feature_name: str, enabled: bool) -> None:
        """Increment the ``yes``/``no`` counter for a feature evaluation.

        :param feature_name: The evaluated feature.
        :param enabled: Whether the feature evaluated enabled (``yes``) or not (``no``).
        """
        with self._lock:
            entry = self._ensure_toggle(feature_name)
            entry["yes" if enabled else "no"] += 1

    def count_variant(self, feature_name: str, variant_name: str) -> None:
        """Increment the per-variant counter for a feature.

        :param feature_name: The evaluated feature.
        :param variant_name: The selected variant name.
        """
        with self._lock:
            entry = self._ensure_toggle(feature_name)
            variants = entry["variants"]
            variants[variant_name] = variants.get(variant_name, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        """Atomically return the accumulated bucket and reset for a new interval.

        :return: The metrics bucket dict (``start``/``stop``/``toggles``). When
            there has been no activity, ``toggles`` is empty.
        """
        with self._lock:
            bucket = {
                "start": self._start,
                "stop": _utc_now_iso(),
                "toggles": self._toggles,
            }
            self._toggles = {}
            self._start = _utc_now_iso()
            return bucket
