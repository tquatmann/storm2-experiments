# Interval MDPs

Comparison of Storm, PRISM and IntervalMDP.jl on interval MDPs (IMDPs). The IMDPs
are derived from the MDP benchmarks in [`../benchmarks/`](../benchmarks) by
widening every transition probability to an interval, and exported explicitly so
that all three tools solve exactly the same model.

| Path | Description |
| --- | --- |
| `generate/` | The generator of the explicit models, its benchmark selection and interval widths, and its tests |
| `benchmarks/` | The generated models, plus `index.json` describing every one of them; only `index.json` is part of the git repository |
| `scripts/` | The configurations compared, in `configurations.json`, and the wrapper that runs IntervalMDP.jl |
| `julia/` | The Julia environment with IntervalMDP.jl 0.7 and the script that solves a model with it |
| `latex/` | The figures, built with pgfplots from the post-processed data (`cd latex && latexmk -pdf main.tex`) |

## Tools

As in the other experiments, the tools are expected in `bin/`:

```bash
cd imdps
mkdir -p bin
ln -s /path/to/storm/build/bin/storm bin/storm
ln -s /path/to/prism/bin/prism bin/prism
ln -s "$(which julia)" bin/julia

# install IntervalMDP.jl into julia/ and compile it once, so that the runs do not
bin/julia --project=julia -e 'using Pkg; Pkg.instantiate(); Pkg.precompile()'
```

Storm has to be 1.13 or newer, PRISM 4.10.1 or newer.

## Generating the models

The models in `benchmarks/` come with the archive of this repository. To generate
them yourself you need Python 3.10+ with stormpy. Use a stormpy built against the
same Storm as `bin/storm`, so that the models are the ones Storm is measured on.

```bash
cd imdps
python3 generate/generate_explicit_benchmarks.py --workers 4
```

This builds every benchmark of `generate/selection.json` (the 60 MDP benchmarks of
the practitioner's guide collection, all in `../benchmarks/index.json`) once for
each interval width in `generate/uncertainty_sets.json`, and writes the bundles to
`benchmarks/<width>/<benchmark>.*` and the list of them to `benchmarks/index.json`.
Benchmarks with more than 10,000,000 states are skipped (`--max-states`). The
limit is checked after the model is built, so the skipped benchmarks still cost
their build time and memory. A model with several million states needs tens of
GB of memory while it is generated; mind this when choosing `--workers`, e.g. by
generating the largest benchmarks separately with `--workers 1`. The bundles take
about 1 GB per million states and width.

Benchmarks minimising an expected reward (`Rmin`) are left out by default
(`--no-skip-min-rewards` includes them): Storm does not support them for interval
models, and neither does IntervalMDP.jl, so only PRISM could be compared on them.

`--benchmark ID` (repeatable) generates only some benchmarks, `--dry-run` lists
the bundles without building anything. Generation overwrites the selected
bundles and rewrites `benchmarks/index.json` from all complete bundles present.

The unit tests are run with

```bash
cd imdps/generate && python3 -m unittest discover -s tests -v
```

## Running the experiments

The steps are those of the other experiments, see the [README](../README.md) in
the root of this repository:

```bash
cd imdps
python3 ../common/generate_invocations.py --out inv.json --timelimit 900 \
    --logdir experiments/logs
python3 ../common/run.py inv.json
python3 ../common/postprocess.py --agreement experiments/logs experiments/results
```

`run.py` links the model files into the temporary directory of an invocation
instead of copying them. IntervalMDP.jl is not run on the reward benchmarks, as its
PRISM importer does not support reachability rewards.

There are no exact results for interval MDPs, so with `--agreement` a result is
compared against those of the other configurations: if at least two and more than
half of the configurations agree on a benchmark (up to the precision of
`postprocess.py`), the median of their results is its reference, and a result
differing from it is incorrect. Benchmarks on which the configurations disagree
are listed. A nominal model (width 0), if configured, is instead compared
against the exact reference result of the original benchmark, which `index.json`
takes over.

The `type` of every benchmark in `index.json` is its interval width, so
`scatter.csv` tells the widths apart.

## Interval construction

`generate/uncertainty_sets.json` contains the absolute widths `0.05` and `0.25`. The intervals come from stormpy's
`AddUncertaintyDouble(model).transform(delta, minimal_value)`, applied to the
nominal model. For each positive nominal probability `p < 1`:

```text
lower = max(p - delta, m)
upper = min(p + delta, 1 - m)
```

and `p = 1` stays `[1,1]`. `m` is the configured `minimal_value` (default `1e-4`,
Storm's default), capped per model at the smallest `min(p, 1 - p)` so that Storm
never rejects a small probability and every interval contains `p`. The value used
is recorded in the `.txt` metadata. Zero-probability edges stay absent, so the
nominal distribution is always feasible and the support is preserved.

IntervalMDP.jl 0.7's PRISM reader requires the same number of choices in every
state. Every export therefore pads to the largest number of choices by
duplicating the first enabled choice, including its rewards. This preserves the
optimal values and introduces no new behaviour, but increases the file sizes and
the work of the solvers; the original and exported choice counts are recorded in
the metadata.

## Model files

Each `benchmarks/<width>/<benchmark>` prefix gets:

| Suffix | Contents |
|---|---|
| `.drn` | Storm interval MDP (`MDP`, `double-interval`) |
| `.storm.props` | Storm query; pair with `--uncertainty-resolution robust` |
| `.tra` | PRISM interval transitions, zero-based state and local choice indices |
| `.sta` | Explicit states with a synthetic state index variable `s` |
| `.lab` | `init`, `deadlock`, `reach`, `avoid` labels |
| `.pctl` | PRISM / IntervalMDP.jl query with `maxmin` / `minmax` quantification |
| `.srew`, `.trew` | State and action rewards, only for reward benchmarks |
| `.txt` | Source, constants, original query, interval width, counts, compatibility |

Nature resolves the intervals adversarially: against a maximising policy it
minimises (`Pmaxmin`), against a minimising one it maximises (`Pminmax`). Storm
expresses this with `--uncertainty-resolution robust` and a plain `Pmax`/`Pmin`
property. Reachability stays a probability query. Safe-until queries become
`!"avoid" U "reach"`, where the avoid states are the unsafe states that are not
targets. Reward queries keep the expected reward until reaching the target, with
state and action rewards; PRISM repeats the action rewards for each successor in
`.trew`. Target and avoid predicates are evaluated on the nominal model, and the
original labels and variables are replaced by the labels above and the state
index.

The `.txt` file is written last and marks a complete bundle. An interrupted run
may leave incomplete model files; regenerate that bundle before using it.

To check a single bundle by hand:

```bash
base=benchmarks/absolute-005/consensus.4-4.disagree
bin/storm --explicit-drn $base.drn --prop $base.storm.props --uncertainty-resolution robust
bin/prism -importmodel $base.tra,sta,lab $base.pctl    # add ,srew,trew for rewards
scripts/intervalmdp.sh $base
```

## Provenance

The generator started from BVI's `community_benchmarks/generate_explicit_benchmarks.py`
(commit `d30ba63efd971b6b661187b366637968f7a1aaf5`), after which the uncertainty
construction and the output were replaced.

Format references:

- [Storm DRN](https://www.stormchecker.org/documentation/background/drn.html)
- [PRISM explicit files](https://www.prismmodelchecker.org/manual/Appendices/ExplicitModelFiles)
- [PRISM uncertain properties](https://www.prismmodelchecker.org/manual/PropertySpecification/UncertainModels)
- [IntervalMDP.jl PRISM importer](https://github.com/Zinoex/IntervalMDP.jl/blob/v0.7.0/src/Data/prism.jl)
