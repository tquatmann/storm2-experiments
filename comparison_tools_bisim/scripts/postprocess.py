#!/usr/bin/env python3
"""Post-process the log files written by run.py.

Reads a directory of log files and writes
  results.json      all data per configuration, benchmark and repetition,
  scatter.csv       median runtime per benchmark and configuration, purely
                    numeric so that it can be read by pgfplots: statuses become
                    the sentinel values below and runtimes are clamped to the
                    plotted range,
  quantile.csv      the sorted median runtimes of each configuration,
  quantile-intersect.csv  the same, but restricted to the benchmarks that every
                    configuration supports.
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

# Exact results are rationals whose numerator and denominator can have tens of
# thousands of digits, well beyond the limit python applies by default.
sys.set_int_max_str_digits(0)

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
PLOT_TIMEOUT = 1200.0
PLOT_INCORRECT = 2400.0
PLOT_NA = 4800.0

# Statuses in decreasing order of severity; the worst one of the repetitions is
# what the csv files report for a benchmark.
STATUSES = ["incorrect", "no-result", "memout", "timeout", "ok"]

# A run killed with SIGKILL although it stayed within its time limit was killed
# from the outside, in practice by the memory limit of the cluster. Such a run is
# reported as a memout rather than as a failure of the tool.
SIGKILL_CODE = -9

# The configuration whose model size is reported as the size of a benchmark. The
# tools disagree here: storm and mcsta build the model for the property at hand
# and drop states that cannot influence it, prism does not. Taking the number
# from one configuration keeps the column comparable across benchmarks.
STATES_CONFIGURATION = "storm-default"

# Configurations whose results are exact and may thus serve as a reference.
EXACT_CONFIGURATIONS = lambda c: "exact" in c

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
    if reference == 0:
        return Fraction(0) if result == 0 else math.inf
    return abs(reference - result) / abs(reference)


def both_near_zero(reference, result):
    """Whether both values are so close to zero that a relative error says little."""
    if reference is None or result is None:
        return False
    if math.isinf(reference) or math.isinf(result):
        return False
    return abs(reference) < ZERO_THRESHOLD and abs(result) < ZERO_THRESHOLD


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
    entry["log"] = str(path)
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
    if difference > GOAL_PRECISION and not both_near_zero(reference, result):
        return "incorrect", difference
    return "ok", difference


def duration(seconds):
    """A number of seconds as a human readable duration."""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


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


def promote_references(entries, index):
    """Fill in missing reference results from the exact configurations.

    Returns the benchmark ids that got a reference result.
    """
    candidates = {}
    for entry in entries:
        benchmark_id = entry["benchmark"]
        if not EXACT_CONFIGURATIONS(entry["configuration"]):
            continue
        if benchmark_id not in index or "reference-result" in index[benchmark_id]:
            continue
        if entry["timeout"] or entry.get("return-code") != 0 or "mcresult" not in entry:
            continue
        candidates.setdefault(benchmark_id, {})[entry["configuration"]] = entry["mcresult"]

    promoted = []
    for benchmark_id, results in sorted(candidates.items()):
        values = {to_number(v) for v in results.values()}
        if len(values) > 1:
            print(f"  not promoting {benchmark_id}: the exact configurations disagree")
            continue
        index[benchmark_id]["reference-result"] = sorted(results.items())[0][1]
        promoted.append(benchmark_id)
    if promoted:
        with open(INDEX_FILE, "w") as f:
            json.dump(index, f, indent="\t", ensure_ascii=False)
            f.write("\n")
    return promoted


HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; margin: 1.5rem; color: #222; }}
h1 {{ font-size: 1.3rem; margin-bottom: 0.2rem; }}
h2 {{ font-size: 1.05rem; margin: 1.4rem 0 0.3rem; }}
.sub {{ color: #666; font-size: 0.85rem; margin: 0.2rem 0; }}
a {{ color: #1a5fb4; }}
{style}
</style>
</head>
<body>
"""

LOG_STYLE = """
pre { background: #f6f6f6; border: 1px solid #ddd; border-radius: 4px;
       padding: 0.7rem; overflow-x: auto; font-size: 0.78rem; line-height: 1.35; }
table.meta { border-collapse: collapse; font-size: 0.85rem; margin-bottom: 0.5rem; }
table.meta th { text-align: left; padding: 0.15rem 0.8rem 0.15rem 0; color: #666;
                 font-weight: normal; vertical-align: top; }
table.meta td { padding: 0.15rem 0; font-family: ui-monospace, monospace;
                 word-break: break-all; max-width: 60rem; }
"""

TABLE_STYLE = """
table { border-collapse: collapse; font-size: 0.82rem; }
th, td { padding: 0.22rem 0.5rem; border-bottom: 1px solid #eee; text-align: right;
          white-space: nowrap; }
th { position: sticky; top: 0; background: #fff; cursor: pointer;
      border-bottom: 2px solid #ccc; user-select: none; }
th:hover { background: #f0f0f0; }
th.sorted::after { content: " \\2191"; color: #888; }
th.sorted.desc::after { content: " \\2193"; }
td:first-child, th:first-child, td:nth-child(2), th:nth-child(2) { text-align: left; }
tbody tr:hover { background: #fafafa; }
td.best { background: #cdf3cd; font-weight: 600; }
td.timeout a, td.memout a { color: #b35c00; }
td.incorrect a { color: #c01c28; }
td.no-result a { color: #777; }
td.na { color: #bbb; }
.hidden { display: none; }
#buttons { margin: 0.6rem 0 1rem; }
button.toggle { font: inherit; font-size: 0.8rem; margin-right: 0.35rem;
                 padding: 0.2rem 0.6rem; border: 1px solid #bbb; border-radius: 999px;
                 background: #fff; color: #888; cursor: pointer; }
button.toggle.on { background: #1a5fb4; border-color: #1a5fb4; color: #fff; }
"""

TABLE_BODY = """<h1>Results</h1>
<p class="sub">Each cell links to the logs of all repetitions. Click a column header to
sort; the fastest runtime of each row is highlighted among the shown configurations.</p>
<div id="buttons">{buttons}</div>
<table id="results" data-fixed="{fixed}">
<thead><tr>{head}</tr></thead>
<tbody>
{body}
</tbody>
</table>
"""

TABLE_SCRIPT = """<script>
const table = document.getElementById("results");
const rows = () => Array.from(table.tBodies[0].rows);

function highlight() {
  for (const row of rows()) {
    let best = null;
    for (const cell of row.cells) {
      cell.classList.remove("best");
      if (cell.dataset.kind !== "time" || cell.classList.contains("hidden")) continue;
      const value = parseFloat(cell.dataset.sort);
      if (best === null || value < parseFloat(best.dataset.sort)) best = cell;
    }
    if (best) best.classList.add("best");
  }
}

function toggleColumn(button) {
  const column = button.dataset.col;
  const on = button.classList.toggle("on");
  for (const cell of table.querySelectorAll('[data-col="' + CSS.escape(column) + '"]'))
    cell.classList.toggle("hidden", !on);
  highlight();
}

let sorted = {index: null, descending: false};
function sortTable(index) {
  const header = table.tHead.rows[0].cells[index];
  const descending = sorted.index === index ? !sorted.descending : false;
  const numeric = header.dataset.kind === "number";
  const body = table.tBodies[0];
  rows().sort((a, b) => {
    const x = a.cells[index].dataset.sort, y = b.cells[index].dataset.sort;
    const cmp = numeric ? parseFloat(x) - parseFloat(y) : x.localeCompare(y);
    return descending ? -cmp : cmp;
  }).forEach(row => body.appendChild(row));
  for (const cell of table.tHead.rows[0].cells) cell.classList.remove("sorted", "desc");
  header.classList.add("sorted");
  if (descending) header.classList.add("desc");
  sorted = {index: index, descending: descending};
}

highlight();
</script>
"""


def escape(text):
    """The text with the characters that are special in html replaced."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def log_page_name(configuration, benchmark_id):
    return f"{configuration}_{benchmark_id}.html"


def write_log_page(path, configuration, benchmark_id, benchmark, repetitions):
    """A page showing the logs of every repetition of one configuration."""
    parts = [HTML_HEAD.format(title=escape(f"{configuration} - {benchmark_id}"),
                              style=LOG_STYLE),
             f"<h1>{escape(benchmark_id)}</h1>",
             f"<p class=\"sub\">{escape(configuration)} &middot; "
             f"{escape(benchmark.get('type', ''))} &middot; "
             f"<a href=\"index.html\">back to the table</a></p>"]
    reference = benchmark.get("reference-result")
    if reference:
        parts.append(f"<p class=\"sub\">reference result: "
                     f"<code>{escape(reference)}</code></p>")
    for repetition in sorted(repetitions, key=int):
        data = repetitions[repetition]
        rows = [("status", data["status"])]
        if "wallclock-time" in data:
            rows.append(("wallclock time", f"{data['wallclock-time']:.3f} s"))
        if "mcresult" in data:
            rows.append(("result", data["mcresult"]))
        if "result-diff" in data:
            rows.append(("relative difference", f"{data['result-diff']:.3g}"))
        if "states" in data:
            rows.append(("states", f"{data['states']:,}"))
        if "states-after" in data:
            rows.append(("states after preprocessing", f"{data['states-after']:,}"))
        parts.append(f"<h2>Repetition {escape(repetition)}</h2>")
        parts.append("<table class=\"meta\">" + "".join(
            f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in rows)
            + "</table>")
        log = Path(data["log"])
        text = log.read_text(errors="replace") if log.is_file() else \
            f"the log file {log} is no longer there"
        parts.append(f"<p class=\"sub\">{escape(log.name)}</p>")
        parts.append(f"<pre>{escape(text)}</pre>")
    parts.append("</body></html>")
    path.write_text("\n".join(parts))


def write_table(outdir, index, results, configurations, benchmarks, cells,
                states, quotient, reducing):
    """An interactive html version of the scatter table, with a page per cell."""
    tabledir = outdir / "table"
    tabledir.mkdir(parents=True, exist_ok=True)

    header = ["benchmark", "type", "states"] + [f"states-{c}" for c in reducing]
    head = "".join(f"<th data-kind=\"{'text' if i < 2 else 'number'}\" "
                   f"onclick=\"sortTable({i})\">{escape(h)}</th>"
                   for i, h in enumerate(header))
    head += "".join(f"<th class=\"cfg\" data-col=\"{escape(c)}\" data-kind=\"number\" "
                    f"onclick=\"sortTable({len(header) + i})\">{escape(c)}</th>"
                    for i, c in enumerate(configurations))

    body = []
    for benchmark_id in benchmarks:
        benchmark = index.get(benchmark_id, {})
        fixed = [(benchmark_id, benchmark_id), (benchmark.get("type", ""), benchmark.get("type", "")),
                 (states.get(benchmark_id, ""), states.get(benchmark_id, -1))]
        for configuration in reducing:
            value = quotient[configuration].get(benchmark_id, "")
            fixed.append((value, value if value != "" else -1))
        row = "".join(f"<td data-sort=\"{escape(key)}\">{escape(text) if text != '' else '&ndash;'}</td>"
                      for text, key in fixed)
        for configuration in configurations:
            repetitions = results[configuration].get(benchmark_id)
            cell = cells.get((benchmark_id, configuration))
            if repetitions is None:
                row += (f"<td class=\"cfg na\" data-col=\"{escape(configuration)}\" "
                        f"data-kind=\"none\" data-sort=\"{PLOT_NA + 1}\">&ndash;</td>")
                continue
            page = log_page_name(configuration, benchmark_id)
            write_log_page(tabledir / page, configuration, benchmark_id, benchmark, repetitions)
            if isinstance(cell, float):
                text, kind, key = f"{cell:.2f}", "time", cell
            else:
                text, kind = cell, cell
                key = {"timeout": PLOT_TIMEOUT, "memout": PLOT_TIMEOUT + 1,
                       "incorrect": PLOT_INCORRECT}.get(cell, PLOT_NA)
            row += (f"<td class=\"cfg {escape(kind)}\" data-col=\"{escape(configuration)}\" "
                    f"data-kind=\"{escape(kind)}\" data-sort=\"{key}\">"
                    f"<a href=\"{escape(page)}\">{escape(text)}</a></td>")
        body.append(f"<tr>{row}</tr>")

    buttons = "".join(
        f"<button class=\"toggle on\" data-col=\"{escape(c)}\" "
        f"onclick=\"toggleColumn(this)\">{escape(c)}</button>" for c in configurations)

    (tabledir / "index.html").write_text(
        HTML_HEAD.format(title="Results", style=TABLE_STYLE)
        + TABLE_BODY.format(buttons=buttons, head=head, body="\n".join(body),
                            fixed=len(header))
        + TABLE_SCRIPT + "</body></html>")
    return tabledir


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", help="directory containing the log files")
    parser.add_argument("out", help="directory the results are written to")
    parser.add_argument("--promote-references", action="store_true",
                        help="store the result of an exact configuration as the reference "
                             "result of a benchmark that does not have one yet")
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
    entries = []
    for path in sorted(logdir.glob("*.log")):
        entry = parse_log(path)
        if entry is None:
            ignored += 1
            continue
        entries.append(entry)

    promoted = promote_references(entries, index) if args.promote_references else []

    for entry in entries:
        benchmark_id = entry["benchmark"]
        if benchmark_id not in index:
            unknown.add(benchmark_id)
        reference = to_number(index.get(benchmark_id, {}).get("reference-result"))
        status, difference = evaluate(entry, reference)

        data = {"status": status, "log": entry["log"]}
        if "wallclock-time" in entry:
            data["wallclock-time"] = entry["wallclock-time"]
        if "mcresult" in entry:
            data["mcresult"] = entry["mcresult"]
        if difference is not None:
            data["result-diff"] = as_float(difference)
        if "states" in entry:
            data["states"] = entry["states"]
            if entry["configuration"] == STATES_CONFIGURATION:
                states[benchmark_id] = entry["states"]
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
                medians.setdefault(configuration, []).append((benchmark_id, median))

    def states_row(benchmark_id):
        return [benchmark_id,
                index.get(benchmark_id, {}).get("type", ""),
                states.get(benchmark_id, "")] + \
               [quotient[c].get(benchmark_id, "") for c in reducing]

    # Every cell is a number that pgfplots can plot.
    with open(outdir / "scatter.csv", "w", newline="") as f:
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
                    row.append(f"{PLOT_TIMEOUT:.0f}")   # out of time or memory
                elif cell == "incorrect":
                    row.append(f"{PLOT_INCORRECT:.0f}")
                else:
                    row.append(f"{PLOT_NA:.0f}")   # no result or not run
            writer.writerow(row)

    # Per configuration the median runtimes in ascending order, padded with nan.
    def write_quantile(name, selection):
        with open(outdir / name, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["i"] + configurations)
            sorted_times = {c: sorted(t for b, t in medians.get(c, [])
                                      if b in selection) for c in configurations}
            for i in range(max((len(v) for v in sorted_times.values()), default=0)):
                row = [i + 1]
                for configuration in configurations:
                    values = sorted_times[configuration]
                    row.append(f"{values[i]:.3f}" if i < len(values) else "nan")
                writer.writerow(row)

    write_quantile("quantile.csv", set(benchmarks))
    # A configuration that cannot be run on a benchmark has no execution for it,
    # for example PRISM on a benchmark that only comes as a jani file.
    supported = {b for b in benchmarks
                 if all(results[c].get(b) for c in configurations)}
    write_quantile("quantile-intersect.csv", supported)

    executions = sum(len(r) for c in results.values() for r in c.values())
    print(f"read {executions} executions of {len(benchmarks)} benchmarks "
          f"in {len(configurations)} configurations from {logdir}")
    counts = {}
    for configuration in results.values():
        for repetitions in configuration.values():
            for data in repetitions.values():
                counts[data["status"]] = counts.get(data["status"], 0) + 1
    print("  " + ", ".join(f"{counts.get(s, 0)} {s}" for s in STATUSES))
    total = sum(data["wallclock-time"]
                for configuration in results.values()
                for repetitions in configuration.values()
                for data in repetitions.values()
                if "wallclock-time" in data)
    print(f"  {total:.1f} s of wallclock time in total, "
          f"i.e. {duration(total)} when run sequentially")
    if ignored:
        print(f"  ignored {ignored} file(s) without a run.py header")
    if unknown:
        print(f"  {len(unknown)} benchmark(s) not in {INDEX_FILE.name}: "
              f"{', '.join(sorted(unknown)[:3])}{' ...' if len(unknown) > 3 else ''}")
    if promoted:
        print(f"  promoted {len(promoted)} result(s) of an exact configuration to a "
              f"reference result in {INDEX_FILE.name}: {', '.join(promoted)}")
    missing = [b for b in benchmarks if b not in states]
    if missing:
        print(f"  no state count from {STATES_CONFIGURATION} for {len(missing)} benchmark(s): "
              f"{', '.join(missing[:3])}{' ...' if len(missing) > 3 else ''}")
    if reducing:
        print(f"  state counts after preprocessing for: {', '.join(reducing)}")
    tabledir = write_table(outdir, index, results, configurations, benchmarks,
                           cells, states, quotient, reducing)
    print(f"  {len(supported)} of {len(benchmarks)} benchmarks are supported by "
          f"every configuration")
    print(f"wrote results.json, scatter.csv, quantile.csv and "
          f"quantile-intersect.csv to {outdir}")
    print(f"wrote the html table to {tabledir / 'index.html'}")


if __name__ == "__main__":
    main()
