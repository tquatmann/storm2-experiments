#!/usr/bin/env python3
"""Generate the list of tool invocations (configuration x benchmark x repetition).

Reads benchmarks/index.json and scripts/configurations.json and writes a JSON array with one
entry per invocation.
"""

import argparse
import json
import random
import sys
from pathlib import Path

from commands import pick_command

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
CONFIGS_FILE = SCRIPT_DIR / "configurations.json"
INDEX_FILE = ROOT / "benchmarks" / "index.json"

# Keys of an index.json entry that are not relevant for running it.
BENCHMARK_SKIP_KEYS = {"reference-result"}


def load_dict(path):
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        sys.exit(f"{path}: expected a JSON object at the top level")
    return data


def select(entries, spec, what):
    """Return the (id, entry) pairs selected by a comma separated list of ids."""
    if spec is None:
        return list(entries.items())
    ids = [i.strip() for i in spec.split(",") if i.strip()]
    unknown = [i for i in ids if i not in entries]
    if unknown:
        sys.exit(f"unknown {what}: {', '.join(unknown)}")
    seen, result = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            result.append((i, entries[i]))
    return result


def with_id(id, entry, skip=()):
    """The entry as a dict with its id as the first key, without the skipped keys."""
    item = {"id": id}
    item.update({k: v for k, v in entry.items() if k != "id" and k not in skip})
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="output file")
    parser.add_argument("--configs", help="comma separated list of configurations (default: all)")
    parser.add_argument("--benchmarks", help="comma separated list of benchmarks (default: all)")
    parser.add_argument("--timelimit", type=int, default=900, help="time limit in seconds (default: 900)")
    parser.add_argument("--logdir", default="logs", help="directory for the logs (default: logs)")
    parser.add_argument("--repetitions", type=int, default=1,
                        help="how often each invocation is repeated (default: 1)")
    parser.add_argument("--noshuffle", action="store_true",
                        help="keep lexicographic order instead of shuffling the output")
    args = parser.parse_args()

    if args.timelimit <= 0:
        sys.exit("--timelimit must be positive")
    if args.repetitions < 1:
        sys.exit("--repetitions must be at least 1")

    all_configs = load_dict(CONFIGS_FILE)
    invalid = [i for i in all_configs if "_" in i]
    if invalid:
        sys.exit(f"{CONFIGS_FILE}: configuration identifiers must not contain '_': "
                 f"{', '.join(invalid)}")

    configs = select(all_configs, args.configs, "configuration")
    benchmarks = select(load_dict(INDEX_FILE), args.benchmarks, "benchmark")
    logdir = args.logdir.rstrip("/")
    created = not Path(logdir).is_dir()
    try:
        Path(logdir).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"cannot create log directory {logdir}: {e}")

    invocations = []
    skipped = 0
    for config_id, config in configs:
        for benchmark_id, benchmark in benchmarks:
            # Skip combinations the configuration cannot be run on, e.g. a tool
            # invoked on a PRISM program for a benchmark that only has a jani file.
            if pick_command(config, benchmark) is None:
                skipped += 1
                continue
            for repetition in range(1, args.repetitions + 1):
                name = f"{config_id}_{benchmark_id}"
                if args.repetitions > 1:
                    name += f"_rep{repetition}"
                invocation = {
                    "config": with_id(config_id, config),
                    "benchmark": with_id(benchmark_id, benchmark, BENCHMARK_SKIP_KEYS),
                    "timelimit": args.timelimit,
                }
                if args.repetitions > 1:
                    invocation["repetition"] = repetition
                invocation["log"] = f"{logdir}/{name}.log"
                invocations.append(invocation)

    if args.noshuffle:
        invocations.sort(key=lambda i: (i["config"]["id"], i["benchmark"]["id"], i.get("repetition", 1)))
    else:
        random.shuffle(invocations)

    with open(args.out, "w") as f:
        json.dump(invocations, f, indent=2)
        f.write("\n")

    print(f"wrote {len(invocations)} invocations "
          f"({len(configs)} configurations x {len(benchmarks)} benchmarks "
          f"x {args.repetitions} repetition(s)) to {args.out}")
    if skipped:
        print(f"  skipped:     {skipped} configuration/benchmark pairs "
              f"the configuration is not applicable to")
    print(f"  time limit:  {args.timelimit}s")
    print(f"  log dir:     {logdir}{' (created)' if created else ''}")
    print(f"  repetitions: {args.repetitions}")


if __name__ == "__main__":
    main()
