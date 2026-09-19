#!/usr/bin/env python
"""
Tests for croniter.snapshot() and croniter.restore().
"""
# -*- coding: utf-8 -*-

import copy
import doctest
import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import zoneinfo
from datetime import datetime

import pytz

from croniter import CroniterSnapshotError, cron_m, croniter
from croniter.tests import base


def reseal(snapshot):
    """Return a copy of *snapshot* with a recomputed digest.

    Used to get past the tamper-evidence check so the schema and consistency
    validators underneath can be exercised directly.
    """
    snapshot = copy.deepcopy(snapshot)
    snapshot["digest"] = cron_m._snapshot_digest(snapshot)
    return snapshot


def aware_key(dt):
    """Comparable identity of an aware datetime: wall clock, offset and fold."""
    return (dt.replace(tzinfo=None), dt.utcoffset(), dt.fold)


class CroniterSnapshotTest(base.TestCase):
    epoch = datetime(2024, 1, 1, 0, 0)

    def test_snapshot_plain_roundtrip(self):
        it = croniter("0 0 * * *", self.epoch)
        for _ in range(3):
            it.get_next()
        restored = croniter.restore(it.snapshot())
        self.assertEqual(restored.get_current(), it.get_current())
        for _ in range(5):
            self.assertEqual(restored.get_next(), it.get_next())
        for _ in range(3):
            self.assertEqual(restored.get_prev(), it.get_prev())

    def test_snapshot_does_not_advance_and_is_idempotent(self):
        it = croniter("*/20 * * * *", self.epoch)
        it.get_next()
        before = it.get_current()
        snap1 = it.snapshot()
        snap2 = it.snapshot()
        self.assertEqual(it.get_current(), before)
        self.assertEqual(snap1, snap2)
        self.assertEqual(
            json.dumps(snap1, sort_keys=True), json.dumps(snap2, sort_keys=True)
        )
        # JSON-serializable plain data only
        self.assertEqual(json.loads(json.dumps(snap1)), snap1)
        # A snapshot in between does not disturb iteration
        parallel = croniter("*/20 * * * *", self.epoch)
        parallel.get_next()
        self.assertEqual(it.get_next(), parallel.get_next())

    def test_snapshot_hash_roundtrip(self):
        it = croniter("H(0-29) H * * *", self.epoch, hash_id="job-42")
        for _ in range(2):
            it.get_next()
        snap = it.snapshot()
        self.assertEqual(snap["hash_id"], {"encoding": "utf-8", "value": "job-42"})
        restored = croniter.restore(json.loads(json.dumps(snap)))
        for _ in range(6):
            self.assertEqual(restored.get_next(), it.get_next())

    def test_snapshot_random_roundtrip(self):
        # R resolves randomly at construction; the snapshot must carry the
        # resolved state so the restored instance continues the same schedule.
        it = croniter("R R R(10-20) * *", self.epoch)
        it.get_next()
        restored = croniter.restore(it.snapshot())
        results = [it.get_next() for _ in range(6)]
        self.assertEqual([restored.get_next() for _ in range(6)], results)
        # An independently constructed instance would (almost surely) differ,
        # so equality above can only come from the snapshot.
        self.assertNotEqual(
            croniter("R R R(10-20) * *", self.epoch).expanded, it.expanded
        )

    def test_snapshot_seven_field_expression(self):
        it = croniter("15 12 * * * 30 2027", datetime(2027, 6, 1))
        for _ in range(2):
            it.get_next()
        restored = croniter.restore(it.snapshot())
        self.assertEqual(len(restored.expanded), 7)
        for _ in range(4):
            self.assertEqual(restored.get_next(), it.get_next())
        for _ in range(2):
            self.assertEqual(restored.get_prev(), it.get_prev())

    def test_snapshot_direction_switching(self):
        ops = ["next", "next", "prev", "next", "next", "prev", "prev", "next"]
        ref = croniter("*/17 * * * *", datetime(2024, 5, 5, 5, 5))
        expected = [getattr(ref, f"get_{op}")() for op in ops]

        it = croniter("*/17 * * * *", datetime(2024, 5, 5, 5, 5))
        for op in ops[:3]:
            getattr(it, f"get_{op}")()
        restored = croniter.restore(it.snapshot())
        got = [getattr(restored, f"get_{op}")() for op in ops[3:]]
        self.assertEqual(got, expected[3:])
        # The iterator protocol follows the stored direction too.
        self.assertEqual(next(restored), next(ref))

    def test_snapshot_options_preserved(self):
        it = croniter(
            "30 0 12 13 * 1",
            self.epoch,
            day_or=False,
            second_at_beginning=True,
            max_years_between_matches=2,
        )
        for _ in range(2):
            it.get_next()
        restored = croniter.restore(it.snapshot())
        self.assertFalse(restored._day_or)
        self.assertTrue(restored.second_at_beginning)
        self.assertEqual(restored._max_years_between_matches, 2)
        self.assertTrue(restored._max_years_btw_matches_explicitly_set)
        for _ in range(3):
            self.assertEqual(restored.get_next(), it.get_next())

    def test_snapshot_dst_spring_forward_new_york(self):
        tz = zoneinfo.ZoneInfo("America/New_York")
        start = datetime(2024, 3, 8, 12, 0, tzinfo=tz)
        # 2024-03-10 02:30 does not exist (clocks jump 02:00 -> 03:00).
        ops = ["next", "next", "next", "prev", "next", "prev", "next"]
        ref = croniter("30 2 * * *", start, ret_type=datetime)
        expected = [aware_key(getattr(ref, f"get_{op}")(datetime)) for op in ops]

        it = croniter("30 2 * * *", start, ret_type=datetime)
        for op in ops[:2]:
            getattr(it, f"get_{op}")(datetime)
        restored = croniter.restore(it.snapshot())
        got = [aware_key(getattr(restored, f"get_{op}")(datetime)) for op in ops[2:]]
        self.assertEqual(got, expected[2:])

    def test_snapshot_dst_fall_back_fold_new_york(self):
        tz = zoneinfo.ZoneInfo("America/New_York")
        start = datetime(2024, 11, 2, 0, 0, tzinfo=tz)
        # 2024-11-03 01:30 happens twice (fold 0 in EDT, fold 1 in EST).
        it = croniter("30 1 * * *", start, ret_type=datetime)
        seen = [aware_key(it.get_next(datetime)) for _ in range(3)]
        self.assertEqual(
            [fold for _, _, fold in seen],
            [0, 0, 1],
            "expected to cross the repeated 01:30 with both folds",
        )
        # Snapshot while the cursor sits on the ambiguous repeated hour.
        restored = croniter.restore(it.snapshot())
        for _ in range(4):
            self.assertEqual(
                aware_key(restored.get_next(datetime)), aware_key(it.get_next(datetime))
            )
        for _ in range(3):
            self.assertEqual(
                aware_key(restored.get_prev(datetime)), aware_key(it.get_prev(datetime))
            )

    def test_snapshot_dst_pytz_roundtrip(self):
        tz = pytz.timezone("America/New_York")
        start = tz.localize(datetime(2024, 3, 8, 12, 0))
        it = croniter("30 2 * * *", start, ret_type=datetime)
        it.get_next(datetime)
        snap = it.snapshot()
        self.assertEqual(
            snap["timezone"], {"kind": "pytz", "name": "America/New_York"}
        )
        restored = croniter.restore(json.loads(json.dumps(snap)))
        for _ in range(4):
            self.assertEqual(
                aware_key(restored.get_next(datetime)), aware_key(it.get_next(datetime))
            )

    def test_snapshot_reject_unknown_version(self):
        snap = reseal(dict(croniter("0 0 * * *", self.epoch).snapshot(), version=999))
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(snap)
        snap = reseal(dict(croniter("0 0 * * *", self.epoch).snapshot(), version="1"))
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(snap)

    def test_snapshot_reject_missing_and_unknown_fields(self):
        snap = croniter("0 0 * * *", self.epoch).snapshot()
        for key in ("cur", "timezone", "options"):
            broken = reseal({k: v for k, v in snap.items() if k != key})
            with self.assertRaises(CroniterSnapshotError, msg=f"missing {key}"):
                croniter.restore(broken)
        broken = {k: v for k, v in snap.items() if k != "digest"}
        with self.assertRaises(CroniterSnapshotError, msg="missing digest"):
            croniter.restore(broken)
        broken = reseal(dict(snap, surprise=1))
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(broken)

    def test_snapshot_reject_tampered_fields(self):
        it = croniter("0 0 * * *", self.epoch)
        it.get_next()
        snap = it.snapshot()
        # Any modification invalidates the digest.
        for key, value in (
            ("cur", snap["cur"] + 3600),
            ("is_prev", True),
            ("fold", 1),
            ("ret_type", "datetime"),
        ):
            broken = dict(snap)
            broken[key] = value
            with self.assertRaises(CroniterSnapshotError, msg=f"tampered {key}"):
                croniter.restore(broken)
        # Even with a recomputed digest, out-of-range values are rejected.
        broken = reseal(snap)
        broken["expanded"][0] = [99]
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(broken)
        broken = reseal(snap)
        broken["expanded"][1] = ["l"]  # 'l' is only valid in the day field
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(broken)
        broken = reseal(snap)
        broken["options"]["day_or"] = "yes"
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(broken)
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore("not a snapshot")
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore({"kind": "something-else"})

    def test_snapshot_reject_inconsistent_fold(self):
        tz = zoneinfo.ZoneInfo("America/New_York")
        start = datetime(2024, 11, 2, 0, 0, tzinfo=tz)
        it = croniter("30 1 * * *", start, ret_type=datetime)
        for _ in range(3):  # land on the repeated 01:30 with fold == 1
            it.get_next(datetime)
        snap = it.snapshot()
        self.assertEqual(snap["fold"], 1)
        broken = reseal(snap)
        broken["fold"] = 0
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(broken)

    def test_snapshot_timezone_mismatch_rejected(self):
        ny = zoneinfo.ZoneInfo("America/New_York")
        it = croniter("0 0 * * *", datetime(2024, 1, 1, tzinfo=ny))
        snap = it.snapshot()
        # Same rules: accepted, whether restored implicitly or explicitly.
        croniter.restore(snap)
        croniter.restore(snap, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
        # Different rules: rejected, never silently converted.
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(snap, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(snap, tzinfo=pytz.timezone("America/New_York"))
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(snap, tzinfo=None)
        # Naive snapshots reject a caller-provided timezone too.
        naive_snap = croniter("0 0 * * *", self.epoch).snapshot()
        croniter.restore(naive_snap, tzinfo=None)
        with self.assertRaises(CroniterSnapshotError):
            croniter.restore(naive_snap, tzinfo=ny)

    def test_snapshot_json_roundtrip_between_processes(self):
        expr = "H/10 * * * *"
        start = "datetime(2024, 1, 1, 0, 0)"
        producer = textwrap.dedent(
            f"""
            import json
            from datetime import datetime
            from croniter import croniter

            it = croniter({expr!r}, {start}, hash_id="snapshot-job")
            for _ in range(5):
                it.get_next()
            print(json.dumps(it.snapshot(), sort_keys=True))
            """
        )
        consumer = textwrap.dedent(
            """
            import json
            import sys
            from croniter import croniter

            it = croniter.restore(json.loads(sys.stdin.read()))
            print(json.dumps([it.get_next() for _ in range(5)]))
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            producer_py = pathlib.Path(tmp, "producer.py")
            consumer_py = pathlib.Path(tmp, "consumer.py")
            producer_py.write_text(producer)
            consumer_py.write_text(consumer)
            produced = subprocess.run(
                [sys.executable, str(producer_py)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            consumed = subprocess.run(
                [sys.executable, str(consumer_py)],
                input=produced,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        # Reference run in this process.
        ref = croniter(expr, datetime(2024, 1, 1, 0, 0), hash_id="snapshot-job")
        for _ in range(5):
            ref.get_next()
        # The snapshot is byte-identical across processes.
        self.assertEqual(produced, json.dumps(ref.snapshot(), sort_keys=True))
        # The other process resumed at the exact position.
        self.assertEqual(json.loads(consumed), [ref.get_next() for _ in range(5)])
        # And restore accepts the JSON string form directly.
        self.assertEqual(croniter.restore(produced).get_next(), json.loads(consumed)[0])

    def test_snapshot_docstring_examples(self):
        finder = doctest.DocTestFinder()
        runner = doctest.DocTestRunner()
        for func in (croniter.snapshot, croniter.restore):
            for test in finder.find(func, f"croniter.{func.__name__}"):
                runner.run(test)
        self.assertEqual(
            runner.failures,
            0,
            f"docstring examples failed ({runner.failures}/{runner.tries})",
        )
        self.assertGreater(runner.tries, 0)

    def test_snapshot_readme_checkpoint_example(self):
        readme = pathlib.Path(__file__).resolve().parents[3] / "README.rst"
        text = readme.read_text(encoding="utf-8")
        section = text.split("Snapshots and checkpoints", 1)[1]
        block = section.split(".. code-block:: python", 1)[1]
        lines = []
        for line in block.splitlines()[1:]:
            if line.strip() and not line.startswith("    "):
                break
            lines.append(line)
        code = textwrap.dedent("\n".join(lines))
        namespace = {}
        exec(compile(code, "README.rst checkpoint example", "exec"), namespace)
        self.assertIn("restored", namespace)


if __name__ == "__main__":
    import unittest

    unittest.main()
