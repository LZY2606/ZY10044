import datetime
import doctest
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from croniter import (
    SNAPSHOT_VERSION,
    CroniterSnapshotError,
    croniter,
)
from croniter.tests import base

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = Path(__file__).resolve().parents[2]

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def json_roundtrip(snapshot):
    """Simulate persisting a snapshot as JSON and loading it back."""
    return json.loads(json.dumps(snapshot))


class CroniterSnapshotTest(base.TestCase):
    def test_snapshot_roundtrip_basic(self):
        cron = croniter(
            "*/20 3 * * *", datetime.datetime(2024, 1, 1, 0, 0), ret_type=datetime.datetime
        )
        cron.get_next()
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        for _ in range(6):
            self.assertEqual(
                restored.get_next(datetime.datetime), cron.get_next(datetime.datetime)
            )

    def test_snapshot_is_stable_and_does_not_advance(self):
        cron = croniter("*/15 * * * *", datetime.datetime(2024, 1, 1, 0, 0))
        cron.get_next()
        before = cron.get_current()
        snap1 = cron.snapshot()
        snap2 = cron.snapshot()
        self.assertEqual(snap1, snap2)
        self.assertEqual(json.dumps(snap1, sort_keys=True), json.dumps(snap2, sort_keys=True))
        self.assertEqual(cron.get_current(), before)
        # A restored instance snapshots back to the very same bytes.
        restored = croniter.from_snapshot(json_roundtrip(snap1))
        self.assertEqual(
            json.dumps(restored.snapshot(), sort_keys=True),
            json.dumps(snap1, sort_keys=True),
        )

    def test_snapshot_hash_expression(self):
        cron = croniter(
            "H/13 H(0-5) * * *",
            datetime.datetime(2024, 1, 1, 0, 0),
            hash_id="snapshot-hash",
            ret_type=datetime.datetime,
        )
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        self.assertEqual(restored._hash_id, b"snapshot-hash")
        for _ in range(8):
            self.assertEqual(
                restored.get_next(datetime.datetime), cron.get_next(datetime.datetime)
            )

    def test_snapshot_hash_id_bytes_not_utf8(self):
        cron = croniter(
            "H * * * *",
            datetime.datetime(2024, 1, 1, 0, 0),
            hash_id=b"\xff\xfe\x00binary",
            ret_type=datetime.datetime,
        )
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        self.assertEqual(restored._hash_id, b"\xff\xfe\x00binary")
        self.assertEqual(restored.get_next(datetime.datetime), cron.get_next(datetime.datetime))

    def test_snapshot_random_expression(self):
        # The random (R) resolution is frozen at expansion time; the snapshot
        # must carry it so the restored instance replays the same schedule.
        cron = croniter(
            "R R R(10-20) * *", datetime.datetime(2024, 1, 1, 0, 0), ret_type=datetime.datetime
        )
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        for _ in range(10):
            self.assertEqual(
                restored.get_next(datetime.datetime), cron.get_next(datetime.datetime)
            )

    def test_snapshot_seven_fields(self):
        cron = croniter(
            "0 0 1 1 * 0 2025-2040/2",
            datetime.datetime(2024, 6, 1, 0, 0),
            ret_type=datetime.datetime,
        )
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        expected = [cron.get_next(datetime.datetime) for _ in range(3)]
        actual = [restored.get_next(datetime.datetime) for _ in range(3)]
        self.assertEqual(actual, expected)
        self.assertEqual([dt.year for dt in actual], [2027, 2029, 2031])

    def test_snapshot_second_at_beginning(self):
        cron = croniter(
            "15,45 * * * * *",
            datetime.datetime(2024, 1, 1, 0, 0, 10),
            ret_type=datetime.datetime,
            second_at_beginning=True,
        )
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        for _ in range(5):
            self.assertEqual(
                restored.get_next(datetime.datetime), cron.get_next(datetime.datetime)
            )

    def test_snapshot_next_prev_switching(self):
        cron = croniter(
            "*/17 * * * *", datetime.datetime(2024, 5, 6, 7, 8), ret_type=datetime.datetime
        )
        cron.get_next()
        cron.get_next()
        cron.get_prev()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        for op in ("next", "prev", "next", "next", "prev", "prev", "next", "prev"):
            expected = getattr(cron, f"get_{op}")(datetime.datetime)
            actual = getattr(restored, f"get_{op}")(datetime.datetime)
            self.assertEqual(actual, expected)

    def test_snapshot_options_preserved(self):
        # day_or=False turns DOM/DOW into an intersection.
        cron = croniter(
            "0 0 13 * 5",
            datetime.datetime(2024, 1, 1, 0, 0),
            ret_type=datetime.datetime,
            day_or=False,
        )
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        self.assertFalse(restored._day_or)
        for _ in range(4):
            self.assertEqual(
                restored.get_next(datetime.datetime), cron.get_next(datetime.datetime)
            )
        # ret_type survives the round trip.
        self.assertIsInstance(restored.get_next(), datetime.datetime)
        cron_float = croniter("0 0 * * *", datetime.datetime(2024, 1, 1, 0, 0))
        restored_float = croniter.from_snapshot(json_roundtrip(cron_float.snapshot()))
        self.assertIsInstance(restored_float.get_next(), float)

    @unittest.skipIf(pytz is None, "pytz is required")
    def test_snapshot_dst_spring_forward_america_new_york(self):
        tz = pytz.timezone("America/New_York")
        # 2024-03-10 02:30 does not exist in America/New_York.
        cron = croniter(
            "30 2 * * *",
            tz.localize(datetime.datetime(2024, 3, 9, 0, 0)),
            ret_type=datetime.datetime,
        )
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        for _ in range(5):
            expected = cron.get_next(datetime.datetime)
            actual = restored.get_next(datetime.datetime)
            self.assertEqual(actual, expected)
            self.assertEqual(actual.utcoffset(), expected.utcoffset())
        # And backwards across the gap as well.
        for _ in range(3):
            self.assertEqual(
                restored.get_prev(datetime.datetime), cron.get_prev(datetime.datetime)
            )

    @unittest.skipIf(pytz is None, "pytz is required")
    def test_snapshot_dst_fall_back_america_new_york(self):
        tz = pytz.timezone("America/New_York")
        # 2024-11-03 01:30 happens twice (fold 0 and fold 1).
        cron = croniter(
            "30 1 * * *",
            tz.localize(datetime.datetime(2024, 11, 2, 0, 0)),
            ret_type=datetime.datetime,
        )
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        results = []
        for _ in range(4):
            expected = cron.get_next(datetime.datetime)
            actual = restored.get_next(datetime.datetime)
            self.assertEqual(actual, expected)
            self.assertEqual(actual.fold, expected.fold)
            results.append(expected)
        # The repeated wall clock must really show up with both UTC offsets
        # (pytz distinguishes the two occurrences by offset, not by fold).
        ambiguous = [dt for dt in results if (dt.month, dt.day, dt.hour) == (11, 3, 1)]
        self.assertEqual(len({dt.utcoffset() for dt in ambiguous}), 2)
        self.assertEqual(
            {dt.utcoffset() for dt in ambiguous},
            {datetime.timedelta(hours=-5), datetime.timedelta(hours=-4)},
        )

    @unittest.skipIf(ZoneInfo is None, "zoneinfo is required")
    def test_snapshot_dst_zoneinfo_america_new_york(self):
        tz = ZoneInfo("America/New_York")
        cron = croniter(
            "30 1 * * *",
            datetime.datetime(2024, 11, 2, 0, 0, tzinfo=tz),
            ret_type=datetime.datetime,
        )
        cron.get_next()
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        results = []
        for _ in range(4):
            expected = cron.get_next(datetime.datetime)
            actual = restored.get_next(datetime.datetime)
            self.assertEqual(actual, expected)
            self.assertEqual(actual.fold, expected.fold)
            results.append(actual)
        # With zoneinfo the repeated wall clock carries both fold values.
        ambiguous = [dt for dt in results if (dt.month, dt.day, dt.hour) == (11, 3, 1)]
        self.assertEqual({dt.fold for dt in ambiguous}, {0, 1})

    def test_snapshot_fixed_offset_timezone(self):
        tz = datetime.timezone(datetime.timedelta(hours=9))
        cron = croniter(
            "0 9 * * *", datetime.datetime(2024, 1, 1, 0, 0, tzinfo=tz), ret_type=datetime.datetime
        )
        restored = croniter.from_snapshot(json_roundtrip(cron.snapshot()))
        self.assertEqual(restored.tzinfo, tz)
        for _ in range(3):
            self.assertEqual(
                restored.get_next(datetime.datetime), cron.get_next(datetime.datetime)
            )

    def test_snapshot_unknown_version_rejected(self):
        cron = croniter("0 0 * * *", datetime.datetime(2024, 1, 1, 0, 0))
        snap = cron.snapshot()
        snap["version"] = SNAPSHOT_VERSION + 100
        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(snap)
        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot({})
        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot("not a dict")

    def test_snapshot_missing_field_rejected(self):
        cron = croniter("0 0 * * *", datetime.datetime(2024, 1, 1, 0, 0))
        for field in ("expanded", "current", "timezone", "options", "checksum"):
            snap = cron.snapshot()
            del snap[field]
            with self.assertRaises(CroniterSnapshotError, msg=f"field={field}"):
                croniter.from_snapshot(snap)

    def test_snapshot_tampered_field_rejected(self):
        cron = croniter("0 0 * * *", datetime.datetime(2024, 1, 1, 0, 0))

        def tamper(snap):
            snap["current"] += 3600.0
            return snap

        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(tamper(cron.snapshot()))

        def tamper_expanded(snap):
            snap["expanded"][0] = [7]
            return snap

        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(tamper_expanded(cron.snapshot()))

        def tamper_checksum(snap):
            snap["checksum"] = "0" * 64
            return snap

        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(tamper_checksum(cron.snapshot()))

        def tamper_options(snap):
            snap["options"]["day_or"] = not snap["options"]["day_or"]
            return snap

        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(tamper_options(cron.snapshot()))

    @unittest.skipIf(pytz is None, "pytz is required")
    def test_snapshot_incompatible_timezone_rejected(self):
        tz = pytz.timezone("America/New_York")
        cron = croniter("30 2 * * *", tz.localize(datetime.datetime(2024, 3, 9, 0, 0)))
        snap = json_roundtrip(cron.snapshot())
        # Different rules: must not silently convert.
        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(snap, tzinfo=pytz.timezone("Europe/Paris"))
        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(snap, tzinfo=datetime.timezone.utc)
        # Same rules, even from another provider: accepted.
        restored = croniter.from_snapshot(snap, tzinfo=pytz.timezone("America/New_York"))
        self.assertEqual(restored.get_current(), cron.get_current())
        if ZoneInfo is not None:
            restored = croniter.from_snapshot(snap, tzinfo=ZoneInfo("America/New_York"))
            self.assertEqual(restored.get_current(), cron.get_current())
        # A naive snapshot is incompatible with any caller-supplied tzinfo.
        naive_snap = json_roundtrip(
            croniter("0 0 * * *", datetime.datetime(2024, 1, 1, 0, 0)).snapshot()
        )
        with self.assertRaises(CroniterSnapshotError):
            croniter.from_snapshot(naive_snap, tzinfo=pytz.timezone("America/New_York"))

    def test_snapshot_two_process_json_roundtrip(self):
        # One process checkpoints to JSON on stdout; a second, fresh process
        # restores from that JSON and continues the iteration.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        producer = textwrap.dedent(
            """
            import datetime, json, sys
            from croniter import croniter
            cron = croniter(
                "H(0-30)/7 * * * *",
                datetime.datetime(2024, 3, 8, 12, 0),
                hash_id="two-process",
            )
            for _ in range(5):
                cron.get_next()
            sys.stdout.write(json.dumps(cron.snapshot()))
            """
        )
        consumer = textwrap.dedent(
            """
            import json, sys
            from croniter import croniter
            snap = json.loads(sys.stdin.read())
            cron = croniter.from_snapshot(snap)
            sys.stdout.write(json.dumps([cron.get_next(float) for _ in range(5)]))
            """
        )
        produced = subprocess.run(
            [sys.executable, "-c", producer],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        consumed = subprocess.run(
            [sys.executable, "-c", consumer],
            input=produced.stdout,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        actual = json.loads(consumed.stdout)
        cron = croniter(
            "H(0-30)/7 * * * *",
            datetime.datetime(2024, 3, 8, 12, 0),
            hash_id="two-process",
        )
        for _ in range(5):
            cron.get_next()
        expected = [cron.get_next(float) for _ in range(5)]
        self.assertEqual(actual, expected)

    def test_snapshot_docstring_examples(self):
        # The examples in the public docstrings are real, executable contracts.
        module = sys.modules["croniter.croniter"]
        results = doctest.testmod(module, optionflags=doctest.ELLIPSIS, verbose=False)
        self.assertEqual(results.failed, 0)
        self.assertGreater(results.attempted, 0)

    def test_snapshot_readme_checkpoint_example(self):
        # The checkpoint example in README.rst is executed, not just prose.
        readme = REPO_ROOT / "README.rst"
        text = readme.read_text(encoding="utf-8")
        section = text.split("Snapshots and checkpointing", 1)[1]
        block = section.split(".. code-block:: python", 1)[1]
        lines = []
        for line in block.splitlines()[1:]:
            if line.strip() and not line.startswith("    "):
                break
            lines.append(line[4:] if line.strip() else "")
        code = "\n".join(lines).strip() + "\n"
        namespace = {}
        exec(compile(code, str(readme), "exec"), namespace)  # noqa: S102
        self.assertIn("resumed", namespace)
        self.assertIn("checkpoint", namespace)


if __name__ == "__main__":
    unittest.main()
