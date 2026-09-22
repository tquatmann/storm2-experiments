#!/usr/bin/env python3
"""Post-process the log files written by run.py.

Reads a directory of log files and writes
  results.json      all data per configuration, benchmark and repetition,
  scatter.csv       median runtime (or status) per benchmark and configuration,
  quantile.csv      the sorted median runtimes of each configuration,
  scatter-plot.csv  the same data as scatter.csv, but purely numeric so that it
                    can be read by pgfplots: statuses become the sentinel values
                    below and runtimes are clamped to the plotted range.
"""

import argparse
import csv
import json
import math
import re
import statistics
import sys
from fractions import Fraction
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
INDEX_FILE = ROOT / "benchmarks" / "index.json"

# Relative precision a result has to meet to count as correct.
GOAL_PRECISION = Fraction(1, 1000)

# Results below this are considered to be zero, so that a tiny absolute deviation
# from a zero reference result is not reported as an infinite relative error.
ZERO_THRESHOLD = Fraction(1, 10**8)

# Where the non-numeric outcomes are drawn in the scatter plots, and the range
# runtimes are clamped to. Keep these in sync with the axes in latex/plots.tex.
PLOT_MIN = 1.0
PLOT_TIMEOUT = 1400.0
PLOT_NA = 4000.0

# Statuses in decreasing order of severity; the worst one of the repetitions is
# what the csv files report for a benchmark.
STATUSES = ["incorrect", "no-result", "memout", "timeout", "ok"]

# A run killed with SIGKILL although it stayed within its time limit was killed
# from the outside, in practice by the memory limit of the cluster. Such a run is
# reported as a memout rather than as a failure of the tool.
SIGKILL_CODE = -9

# The value a tool reports for the checked property. The first group is the value.
RESULT_PATTERNS = [
    re.compile(r"^Result \(for initial states\):\s*(\S+)", re.M),   # storm
    re.compile(r"^Result:\s*([^\s(]+)", re.M),                      # prism
    re.compile(r"^\s+(?:Probability|Value):\s*(\S+)", re.M),        # mcsta
]

# The size of the model reported by the tool. Storm prints this once per model
# info block, so a second occurrence is the model after preprocessing, e.g. the
# quotient of a bisimulation minimisation.
STATES_PATTERNS = [
    re.compile(r"^States:[ \t]+(\d+)", re.M),      # storm, prism
    re.compile(r"^[ \t]+States:[ \t]+(\d+)", re.M),  # mcsta
]

HEADER = re.compile(r"^(?P<key>[A-Za-z ]+):\t(?P<value>.*)$", re.M)
FOOTER = re.compile(r"^Wallclock time:\t(?P<time>[0-9.]+)\nReturn code:\t(?P<code>.*)$", re.M)


def to_number(text):
    """The given string as an exact number, or None if it is not one."""
    if text is None:
        return None
    text = text.strip().rstrip(",;")
    if text.lower().lstrip("+") in ("inf", "infinity"):
        return math.inf
    if text.lower() == "-infinity" or text.lower() == "-inf":
        return -math.inf
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        pass
    try:
        return Fraction(float(text))   # scientific notation
    except (ValueError, OverflowError):
        return None


def relative_difference(reference, result):
    """The relative difference between a reference result and a tool result."""
    if reference is None or result is None:
        return None
    if math.isinf(reference) and math.isinf(result):
        return Fraction(0) if (reference > 0) == (result > 0) else math.inf
    if math.isinf(reference) or math.isinf(result):
        return math.inf
    if abs(reference) < ZERO_THRESHOLD:
        return Fraction(0) if abs(result) < ZERO_THRESHOLD else math.inf
    return abs(reference - result) / abs(reference)


def parse_log(path):
    """The contents of one log file, or None if it is not a log written by run.py."""
    text = path.read_text(errors="replace")
    head, _, body = text.partition("\nOutput:\n")
    header = {m.group("key"): m.group("value") for m in HEADER.finditer(head)}
    if "Benchmark" not in header or "Configuration" not in header:
        return None

    footer = None
    for footer in FOOTER.finditer(body):
        pass   # the tool may print similar lines itself, so keep the last one
    output = body[:footer.start()] if footer else body

    entry = {
        "benchmark": header["Benchmark"],
        "configuration": header["Configuration"],
        "repetition": int(header.get("Repetition", 1)),
        "timelimit": to_number(header.get("Time limit")),
    }
    if footer:
        entry["wallclock-time"] = float(footer.group("time"))
        entry["return-code"] = None if footer.group("code") == "None" else int(footer.group("code"))
    else:
        entry["return-code"] = None

    for pattern in RESULT_PATTERNS:
        match = pattern.search(output)
        if match:
            entry["mcresult"] = match.group(1)
            break
    for pattern in STATES_PATTERNS:
        found = pattern.findall(output)
        if found:
            entry["states"] = int(found[0])
            if len(found) > 1:
                entry["states-after"] = int(found[-1])
            break
    # A log without a footer means the run never finished properly.
    entry["timeout"] = footer is not None and footer.group("code") == "None"
    return entry


def evaluate(entry, reference):
    """Status, result and relative difference of a single execution."""
    if entry["timeout"]:
        return "timeout", None
    if entry.get("return-code") == SIGKILL_CODE:
        return "memout", None
    result = to_number(entry.get("mcresult"))
    if result is None:
        return "no-result", None
    difference = relative_difference(reference, result)
    if difference is None:
        return "ok", None          # nothing to compare against
    if difference > GOAL_PRECISION:
        return "incorrect", difference
    return "ok", difference


def as_float(value):
    """A Fraction, an int or an infinity as a float."""
    if value is None:
        return None
    return float(value)


def worst_status(statuses):
    for status in STATUSES:
        if status in statuses:
            return status
    return "no-result"


def median_runtime(repetitions):
    """The median wallclock time of the repetitions, if all of them are ok."""
    statuses = {r["status"] for r in repetitions.values()}
    if statuses != {"ok"}:
        return None
    times = [r["wallclock-time"] for r in repetitions.values() if "wallclock-time" in r]
    return statistics.median(times) if times else None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", help="directory containing the log files")
    parser.add_argument("out", help="directory the results are written to")
    args = parser.parse_args()

    logdir, outdir = Path(args.logs), Path(args.out)
    if not logdir.is_dir():
        sys.exit(f"not a directory: {logdir}")
    outdir.mkdir(parents=True, exist_ok=True)

    with open(INDEX_FILE) as f:
        index = json.load(f)

    results = {}      # configuration -> benchmark -> repetition -> data
    states = {}       # benchmark -> number of states of the input model
    quotient = {}     # configuration -> benchmark -> states after preprocessing
    unknown, ignored = set(), 0
    for path in sorted(logdir.glob("*.log")):
        entry = parse_log(path)
        if entry is None:
            ignored += 1
            continue
        benchmark_id = entry["benchmark"]
        if benchmark_id not in index:
            unknown.add(benchmark_id)
        reference = to_number(index.get(benchmark_id, {}).get("reference-result"))
        status, difference = evaluate(entry, reference)

        data = {"status": status}
        if "wallclock-time" in entry:
            data["wallclock-time"] = entry["wallclock-time"]
        if "mcresult" in entry:
            data["mcresult"] = entry["mcresult"]
        if difference is not None:
            data["result-diff"] = as_float(difference)
        if "states" in entry:
            data["states"] = entry["states"]
            states[benchmark_id] = max(states.get(benchmark_id, 0), entry["states"])
        if "states-after" in entry:
            data["states-after"] = entry["states-after"]
            # Only a configuration that actually reduces the model gets a column.
            if entry["states-after"] != entry["states"]:
                quotient.setdefault(entry["configuration"], {})[benchmark_id] = \
                    entry["states-after"]
        results.setdefault(entry["configuration"], {}) \
               .setdefault(benchmark_id, {})[str(entry["repetition"])] = data

    configurations = sorted(results)
    benchmarks = sorted({b for c in results.values() for b in c})

    with open(outdir / "results.json", "w") as f:
        json.dump(results, f, indent="\t")
        f.write("\n")

    # For each benchmark and configuration either the median runtime or the status.
    reducing = [c for c in configurations if c in quotient]
    columns = ["benchmark", "type", "states"] + [f"states-{c}" for c in reducing]
    medians = {}
    cells = {}        # (benchmark, configuration) -> median runtime or status
    for benchmark_id in benchmarks:
        for configuration in configurations:
            repetitions = results[configuration].get(benchmark_id)
            if not repetitions:
                continue
            median = median_runtime(repetitions)
            if median is None:
                cells[benchmark_id, configuration] = \
                    worst_status({r["status"] for r in repetitions.values()})
            else:
                cells[benchmark_id, configuration] = median
                medians.setdefault(configuration, []).append(median)

    def states_row(benchmark_id):
        return [benchmark_id,
                index.get(benchmark_id, {}).get("type", ""),
                states.get(benchmark_id, "")] + \
               [quotient[c].get(benchmark_id, "") for c in reducing]

    with open(outdir / "scatter.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns + configurations)
        for benchmark_id in benchmarks:
            row = states_row(benchmark_id)
            for configuration in configurations:
                cell = cells.get((benchmark_id, configuration), "")
                row.append(f"{cell:.3f}" if isinstance(cell, float) else cell)
            writer.writerow(row)

    # The same table, but with every cell a number that pgfplots can plot.
    with open(outdir / "scatter-plot.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns + configurations)
        for benchmark_id in benchmarks:
            row = [str(v) if v != "" else "nan" for v in states_row(benchmark_id)]
            row[1] = index.get(benchmark_id, {}).get("type", "unknown")   # the mark class
            for configuration in configurations:
                cell = cells.get((benchmark_id, configuration), "")
                if isinstance(cell, float):
                    row.append(f"{min(max(cell, PLOT_MIN), PLOT_TIMEOUT - 1):.3f}")
                elif cell in ("timeout", "memout"):
                    row.append(f"{PLOT_TIMEOUT:.0f}")   # out of resources
                else:
                    row.append(f"{PLOT_NA:.0f}")   # incorrect, no-result or not run
            writer.writerow(row)

    # Per configuration the median runtimes in ascending order, padded with nan.
    with open(outdir / "quantile.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["i"] + configurations)
        columns = {c: sorted(medians.get(c, [])) for c in configurations}
        for i in range(max((len(v) for v in columns.values()), default=0)):
            row = [i + 1]
            for configuration in configurations:
                values = columns[configuration]
                row.append(f"{values[i]:.3f}" if i < len(values) else "nan")
            writer.writerow(row)

    executions = sum(len(r) for c in results.values() for r in c.values())
    print(f"read {executions} executions of {len(benchmarks)} benchmarks "
          f"in {len(configurations)} configurations from {logdir}")
    counts = {}
    for configuration in results.values():
        for repetitions in configuration.values():
            for data in repetitions.values():
                counts[data["status"]] = counts.get(data["status"], 0) + 1
    print("  " + ", ".join(f"{counts.get(s, 0)} {s}" for s in STATUSES))
    if ignored:
        print(f"  ignored {ignored} file(s) without a run.py header")
    if unknown:
        print(f"  {len(unknown)} benchmark(s) not in {INDEX_FILE.name}: "
              f"{', '.join(sorted(unknown)[:3])}{' ...' if len(unknown) > 3 else ''}")
    if reducing:
        print(f"  state counts after preprocessing for: {', '.join(reducing)}")
    print(f"wrote results.json, scatter.csv, scatter-plot.csv and quantile.csv to {outdir}")


if __name__ == "__main__":
    main()
