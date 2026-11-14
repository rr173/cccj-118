"""Random differential test: the store vs an independent in-memory model.

Every active snapshot is checked against the model at its own commit number
after every step, across interleaved writes, flushes, compacts and
snapshot/iterator churn.
"""

import random
import shutil
import tempfile
import unittest

from distagg.staging import StagingStore
from reference_staging import HistoryModel

KEY_POOL = [
    b"", b"\x00", b"\x00\x00", b"a", b"b", b"c", b"a\x00", b"\xff",
    b"aa", b"ab", b"key", b"k\x00y", b"z", b"zz", b"\x00a", b"a\xff",
]


class RandomDifferentialTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="staging-random-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_random_operations_match_model(self):
        rng = random.Random(20261114)
        store = StagingStore.open(self.dir, memtable_budget=700, block_size=200)
        model = HistoryModel()
        snapshots = []  # list of [Snapshot, commit]

        def check_all(step):
            keys = rng.sample(KEY_POOL, 6)
            for key in keys:
                self.assertEqual(
                    store.get(key), model.get(key, model.commit),
                    f"step {step}: store.get({key!r})",
                )
            for snap, commit in snapshots:
                for key in keys:
                    self.assertEqual(
                        snap.get(key), model.get(key, commit),
                        f"step {step}: snapshot@{commit}.get({key!r})",
                    )

        for step in range(300):
            roll = rng.random()
            if roll < 0.50:
                ops = []
                for _ in range(rng.randint(1, 4)):
                    key = rng.choice(KEY_POOL)
                    if rng.random() < 0.3:
                        ops.append(("delete", key))
                    else:
                        ops.append(("put", key, bytes([rng.randrange(256)]) * rng.randrange(0, 30)))
                commit = store.write_batch(ops)
                self.assertEqual(commit, model.apply_batch(ops))
            elif roll < 0.62:
                store.flush()
            elif roll < 0.72:
                store.compact()
            elif roll < 0.82:
                if len(snapshots) < 6:
                    snapshots.append([store.snapshot(), store.commit_number])
            elif roll < 0.90:
                if snapshots:
                    snap, _ = snapshots.pop(rng.randrange(len(snapshots)))
                    snap.close()
            else:
                # Compare a full scan of the store and of every live snapshot.
                lower = rng.choice([None] + KEY_POOL)
                upper = rng.choice([None] + KEY_POOL)
                if lower is not None and upper is not None and lower > upper:
                    lower, upper = upper, lower
                with store.scan(lower=lower, upper=upper) as iterator:
                    self.assertEqual(
                        list(iterator), model.scan(lower, upper, model.commit),
                        f"step {step}: store scan",
                    )
                for snap, commit in snapshots:
                    with snap.scan(lower=lower, upper=upper) as iterator:
                        self.assertEqual(
                            list(iterator), model.scan(lower, upper, commit),
                            f"step {step}: snapshot@{commit} scan",
                        )
            check_all(step)

        # Reopen and verify the committed state survives recovery exactly.
        for snap, _ in snapshots:
            snap.close()
        store.close()
        reopened = StagingStore.open(self.dir)
        self.assertEqual(reopened.commit_number, model.commit)
        with reopened.scan() as iterator:
            self.assertEqual(list(iterator), model.scan(None, None, model.commit))
        reopened.close()


if __name__ == "__main__":
    unittest.main()
