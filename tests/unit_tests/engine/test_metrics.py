"""Unit tests for the embedded engine's metrics bucket.

``UnleashClient.engine.metrics.MetricsBucket`` is a self-contained, thread-safe
accumulator of toggle (yes/no) and variant evaluation counts. It has no
dependency on the rest of the engine, so these tests exercise it directly.

The contract (mirrored from the yggdrasil bucket consumed by
``periodic_tasks/send_metrics.py``):

* ``count_toggle(name, enabled)`` increments a per-toggle ``yes``/``no`` counter.
* ``count_variant(name, vname)`` increments a per-toggle, per-variant counter.
* ``snapshot()`` returns ``{"start", "stop", "toggles": {name: {"yes", "no",
  "variants": {vname: int}}}}`` and atomically resets the interval.
* When nothing has been recorded, ``toggles`` is empty (the facade treats this
  as falsy so ``send_metrics`` skips submission).
* ``start``/``stop`` are UTC ISO8601 strings terminated with ``Z``.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from UnleashClient.engine import UnleashEngine
from UnleashClient.engine.metrics import MetricsBucket, _utc_now_iso

# ---------------------------------------------------------------------------
# _utc_now_iso
# ---------------------------------------------------------------------------

ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def test_utc_now_iso_has_trailing_z_and_no_offset():
    value = _utc_now_iso()
    assert value.endswith("Z")
    assert "+00:00" not in value
    assert ISO_Z_RE.match(value), value


def test_utc_now_iso_is_parseable_utc():
    value = _utc_now_iso()
    # Round-trips back through fromisoformat once the trailing Z is normalized.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


# ---------------------------------------------------------------------------
# Empty bucket behaviour
# ---------------------------------------------------------------------------


def test_fresh_bucket_snapshot_has_empty_toggles():
    bucket = MetricsBucket()
    snap = bucket.snapshot()
    assert snap["toggles"] == {}
    assert "start" in snap and "stop" in snap


def test_empty_snapshot_toggles_is_falsy():
    # The facade's get_metrics returns None when toggles is empty; verify the
    # underlying signal it keys off is falsy.
    bucket = MetricsBucket()
    snap = bucket.snapshot()
    assert not snap["toggles"]


# ---------------------------------------------------------------------------
# count_toggle
# ---------------------------------------------------------------------------


def test_count_toggle_enabled_increments_yes():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["yes"] == 1
    assert snap["toggles"]["feature.a"]["no"] == 0


def test_count_toggle_disabled_increments_no():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", False)
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["no"] == 1
    assert snap["toggles"]["feature.a"]["yes"] == 0


def test_count_toggle_accumulates_yes_and_no():
    bucket = MetricsBucket()
    for _ in range(3):
        bucket.count_toggle("feature.a", True)
    for _ in range(2):
        bucket.count_toggle("feature.a", False)
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"] == {"yes": 3, "no": 2, "variants": {}}


def test_count_toggle_tracks_multiple_features_independently():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    bucket.count_toggle("feature.b", False)
    bucket.count_toggle("feature.b", False)
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["yes"] == 1
    assert snap["toggles"]["feature.b"]["no"] == 2


def test_new_toggle_entry_has_variants_dict():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["variants"] == {}


# ---------------------------------------------------------------------------
# count_variant
# ---------------------------------------------------------------------------


def test_count_variant_increments_named_variant():
    bucket = MetricsBucket()
    bucket.count_variant("feature.a", "blue")
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["variants"]["blue"] == 1


def test_count_variant_accumulates_per_variant():
    bucket = MetricsBucket()
    bucket.count_variant("feature.a", "blue")
    bucket.count_variant("feature.a", "blue")
    bucket.count_variant("feature.a", "red")
    snap = bucket.snapshot()
    variants = snap["toggles"]["feature.a"]["variants"]
    assert variants == {"blue": 2, "red": 1}


def test_count_variant_creates_toggle_entry_with_zero_yes_no():
    # A variant count for a feature not yet seen by count_toggle still creates a
    # well-formed toggle entry.
    bucket = MetricsBucket()
    bucket.count_variant("feature.a", "blue")
    snap = bucket.snapshot()
    entry = snap["toggles"]["feature.a"]
    assert entry["yes"] == 0
    assert entry["no"] == 0
    assert entry["variants"] == {"blue": 1}


def test_count_toggle_and_variant_share_one_entry():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    bucket.count_variant("feature.a", "blue")
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"] == {
        "yes": 1,
        "no": 0,
        "variants": {"blue": 1},
    }


def test_disabled_variant_name_is_counted_like_any_other():
    # The disabled variant ("disabled") is recorded by name just like the rest.
    bucket = MetricsBucket()
    bucket.count_variant("feature.a", "disabled")
    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["variants"]["disabled"] == 1


# ---------------------------------------------------------------------------
# snapshot shape
# ---------------------------------------------------------------------------


def test_snapshot_returns_exact_top_level_keys():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    snap = bucket.snapshot()
    assert set(snap.keys()) == {"start", "stop", "toggles"}


def test_snapshot_toggle_entry_has_exact_keys():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    snap = bucket.snapshot()
    assert set(snap["toggles"]["feature.a"].keys()) == {"yes", "no", "variants"}


def test_snapshot_start_and_stop_are_z_iso():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    snap = bucket.snapshot()
    assert ISO_Z_RE.match(snap["start"]), snap["start"]
    assert ISO_Z_RE.match(snap["stop"]), snap["stop"]


def test_snapshot_stop_not_before_start():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    snap = bucket.snapshot()
    start = datetime.fromisoformat(snap["start"].replace("Z", "+00:00"))
    stop = datetime.fromisoformat(snap["stop"].replace("Z", "+00:00"))
    assert stop >= start


# ---------------------------------------------------------------------------
# atomic reset semantics
# ---------------------------------------------------------------------------


def test_snapshot_resets_toggles():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    bucket.snapshot()
    second = bucket.snapshot()
    assert second["toggles"] == {}


def test_counts_after_reset_start_from_zero():
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    bucket.snapshot()
    bucket.count_toggle("feature.a", True)
    second = bucket.snapshot()
    assert second["toggles"]["feature.a"]["yes"] == 1


def test_returned_snapshot_is_detached_from_live_bucket():
    # Mutating the bucket after snapshotting must not retroactively change the
    # already-returned snapshot (snapshot hands ownership of the dict to caller
    # and installs a fresh one).
    bucket = MetricsBucket()
    bucket.count_toggle("feature.a", True)
    snap = bucket.snapshot()
    bucket.count_toggle("feature.a", True)
    assert snap["toggles"]["feature.a"]["yes"] == 1


def test_next_interval_start_advances_after_snapshot():
    bucket = MetricsBucket()
    first = bucket.snapshot()
    # second interval's start should be >= first interval's stop
    second = bucket.snapshot()
    first_stop = datetime.fromisoformat(first["stop"].replace("Z", "+00:00"))
    second_start = datetime.fromisoformat(second["start"].replace("Z", "+00:00"))
    assert second_start >= first_stop


# ---------------------------------------------------------------------------
# thread-safety
# ---------------------------------------------------------------------------


def test_concurrent_count_toggle_loses_no_increments():
    bucket = MetricsBucket()
    threads_count = 8
    per_thread = 2000
    barrier = threading.Barrier(threads_count)

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            bucket.count_toggle("feature.a", True)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["yes"] == threads_count * per_thread


def test_concurrent_count_variant_loses_no_increments():
    bucket = MetricsBucket()
    threads_count = 8
    per_thread = 2000
    barrier = threading.Barrier(threads_count)

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            bucket.count_variant("feature.a", "blue")

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = bucket.snapshot()
    assert snap["toggles"]["feature.a"]["variants"]["blue"] == (
        threads_count * per_thread
    )


def test_concurrent_writes_with_interleaved_snapshots_conserve_total():
    # While writers hammer the bucket, a reader periodically snapshots-and-resets.
    # No increment may be dropped or double counted across the partial buckets.
    bucket = MetricsBucket()
    writers_count = 6
    per_writer = 5000
    barrier = threading.Barrier(writers_count)
    stop_flag = threading.Event()
    collected_yes = []
    collected_lock = threading.Lock()

    def writer():
        barrier.wait()
        for _ in range(per_writer):
            bucket.count_toggle("feature.a", True)

    def reader():
        while not stop_flag.is_set():
            snap = bucket.snapshot()
            entry = snap["toggles"].get("feature.a")
            if entry:
                with collected_lock:
                    collected_yes.append(entry["yes"])

    writers = [threading.Thread(target=writer) for _ in range(writers_count)]
    reader_t = threading.Thread(target=reader)
    reader_t.start()
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop_flag.set()
    reader_t.join()

    # Final drain to catch anything the reader missed after stop_flag.
    final = bucket.snapshot()
    final_entry = final["toggles"].get("feature.a")
    if final_entry:
        collected_yes.append(final_entry["yes"])

    assert sum(collected_yes) == writers_count * per_writer


# ---------------------------------------------------------------------------
# integration with the facade
# ---------------------------------------------------------------------------


def test_facade_get_metrics_returns_none_when_no_activity():

    engine = UnleashEngine()
    assert engine.get_metrics() is None


def test_facade_get_metrics_returns_bucket_after_count():

    engine = UnleashEngine()
    engine.count_toggle("feature.a", True)
    engine.count_variant("feature.a", "blue")
    metrics = engine.get_metrics()
    assert metrics is not None
    assert metrics["toggles"]["feature.a"]["yes"] == 1
    assert metrics["toggles"]["feature.a"]["variants"]["blue"] == 1


def test_facade_get_metrics_resets_between_calls():

    engine = UnleashEngine()
    engine.count_toggle("feature.a", True)
    assert engine.get_metrics() is not None
    # Second call with no new activity is None again.
    assert engine.get_metrics() is None


def test_facade_constructor_does_not_raise():

    # Constructor relies on MetricsBucket() not raising.
    UnleashEngine()
