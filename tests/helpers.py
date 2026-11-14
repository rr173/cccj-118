import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def E(publisher, creative, cost, weight=1):
    return {
        "relation": "E",
        "row": {"publisher": publisher, "creative": creative, "cost": cost},
        "weight": weight,
    }


def T(creative, tag, weight=1):
    return {"relation": "T", "row": {"creative": creative, "tag": tag}, "weight": weight}


def write_txn(path, seq, edits):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"seq": seq, "edits": edits}, handle)
    return path


def cli_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_cli(args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "distagg", *args],
        capture_output=True,
        text=True,
        env=cli_env(),
        **kwargs,
    )


def random_txn(rng, seq, creatives, publishers, tags, costs, max_edits=5):
    edits = []
    for _ in range(rng.randint(0, max_edits)):
        if rng.random() < 0.5:
            edits.append(
                E(
                    rng.choice(publishers),
                    rng.choice(creatives),
                    rng.choice(costs),
                    rng.choice([1, 1, 1, 2, 3, -1, -1, -2]),
                )
            )
        else:
            edits.append(
                T(
                    rng.choice(creatives),
                    rng.choice(tags),
                    rng.choice([1, 1, 1, 2, -1, -1, -2]),
                )
            )
    return {"seq": seq, "edits": edits}
