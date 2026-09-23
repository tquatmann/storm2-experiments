# Storm 2 paper experiments

Benchmarks, scripts and results for the experimental evaluation accompanying the
Storm 2 paper.

## Contents

| Path | Description |
| --- | --- |
| `comparison_tools_bisim/` | Comparison of Storm, mcsta and PRISM, with and without bisimulation minimisation |
| `comparison_tools_bisim/benchmarks/` | The model and property files, plus `index.json` describing every benchmark |
| `comparison_tools_bisim/scripts/` | Scripts to generate, run and post-process the experiments |
| `comparison_tools_bisim/experiments/` | The logs of the recorded run and the results derived from them |
| `comparison_tools_bisim/latex/` | The figures, built with pgfplots from the post-processed data |

## Running the experiments

The tools are expected in `comparison_tools_bisim/bin/` as `storm`, `modest` and
`prism`. This directory is not part of the repository; link the executables of an
existing installation into it, for example

```bash
cd comparison_tools_bisim
mkdir -p bin
ln -s /path/to/storm/build/bin/storm bin/storm
ln -s /path/to/Modest/modest bin/modest
ln -s /path/to/prism/bin/prism bin/prism

# each of these should now print a version
bin/storm --version && bin/modest --version && bin/prism -version
```

Link the launcher scripts of mcsta and PRISM rather than the jar files, so that
they find the rest of their installation. The configurations to compare are
defined in `scripts/configurations.json`.

```bash
cd comparison_tools_bisim

# 1. build the list of invocations (configuration x benchmark x repetition)
python3 scripts/generate_invocations.py --out inv.json --timelimit 900 \
    --logdir experiments/logs

# 2. run them; each invocation gets its own temporary directory and log file
python3 scripts/run.py inv.json

# 3. turn the logs into results.json, scatter.csv, quantile.csv and an html table
python3 scripts/postprocess.py experiments/logs experiments/results

# 4. build the figures
cd latex && latexmk -pdf main.tex
```

`generate_invocations.py` skips configuration/benchmark combinations that cannot
be run, for example a PRISM invocation for a benchmark that only has a JANI
model. `run.py` takes `--entry I` to run a single invocation, which is useful for
distributing the work over a cluster. Both scripts describe their remaining
options with `--help`.

## Results

`postprocess.py` writes

- `results.json` — status, runtime, model checking result and the relative
  difference to the reference result, per configuration, benchmark and repetition,
- `scatter.csv` — median runtime per benchmark and configuration, with failures
  mapped to sentinel values so that pgfplots can plot them,
- `quantile.csv` — the sorted median runtimes of each configuration,
- `table/index.html` — a sortable table linking to the logs of every run.

## License

The contents of this repository are licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); see [LICENSE](LICENSE).

This does not extend to the tools in `comparison_tools_bisim/bin/`, which are not
part of this repository.
