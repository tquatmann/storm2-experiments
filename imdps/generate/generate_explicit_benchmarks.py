#!/usr/bin/env python3
"""Export PRISM/JANI benchmarks as explicit interval MDPs for three solvers.

The benchmarks are looked up in the index.json shared by the experiments of this
repository. Next to the bundles, the index.json of this experiment is written, which
lists every complete bundle for the scripts in common/.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path
from typing import Any


THIS_DIR = Path(__file__).resolve().parent
EXPERIMENT_DIR = THIS_DIR.parent
DEFAULT_BENCHMARK_SET = THIS_DIR / "selection.json"
DEFAULT_UNCERTAINTY_CONFIG = THIS_DIR / "uncertainty_sets.json"
DEFAULT_OUTPUT_DIR = EXPERIMENT_DIR / "benchmarks"
DEFAULT_BENCHMARK_ROOT = EXPERIMENT_DIR.parent / "benchmarks"
DEFAULT_MAX_STATES = 10_000_000

@lru_cache(maxsize=None)
def load_json(path: Path) -> Any:
    """Load read-only JSON metadata once per process."""
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def value_string(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def parse_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def resolve_benchmark(benchmark_root: Path, benchmark_id: str) -> dict[str, Any]:
    """Look a benchmark up in the index.json in benchmark_root."""
    entry = load_json(benchmark_root / "index.json").get(benchmark_id)
    if entry is None:
        raise LookupError(f"unknown benchmark {benchmark_id!r}")
    # Only the constants left open by the model files, e.g. "K=4" for consensus.4.
    constants_string = entry.get("constants", "")
    constants = {}
    for definition in filter(None, constants_string.split(",")):
        name, _, value = definition.partition("=")
        constants[name.strip()] = parse_value(value)

    def path(key: str) -> Path | None:
        return benchmark_root / entry[key] if key in entry else None

    return {
        "id": benchmark_id,
        "property": entry["property"],
        "type": entry["type"],
        "program": path("prism"),
        "jani": path("jani"),
        "properties": path("prism-property"),
        "constants": constants,
        "constants_string": constants_string,
    }


@lru_cache(maxsize=None)
def property_map(path: Path) -> dict[str, str]:
    text = "\n".join(line.split("//", 1)[0] for line in path.read_text().splitlines())
    props: dict[str, str] = {}
    for match in re.finditer(r'"([^"]+)"\s*:\s*(.*?)(?:;|$)', text, re.DOTALL):
        props[match.group(1)] = re.sub(r"\s+", " ", match.group(2)).strip()
    return props


def jani_expression_to_text(expr: Any) -> str:
    if isinstance(expr, str):
        return expr
    if isinstance(expr, bool):
        return str(expr).lower()
    if isinstance(expr, int | float):
        return str(expr)
    if not isinstance(expr, dict):
        raise ValueError(f"unsupported JANI expression {expr!r}")

    op = expr.get("op")
    if op in {"¬", "!"}:
        return f"!({jani_expression_to_text(expr['exp'])})"
    if op in {"∧", "&"}:
        items = expr.get("exp")
        if items is None and "left" in expr and "right" in expr:
            items = [expr["left"], expr["right"]]
        return " & ".join(f"({jani_expression_to_text(item)})" for item in (items or []))
    if op in {"∨", "|"}:
        items = expr.get("exp")
        if items is None and "left" in expr and "right" in expr:
            items = [expr["left"], expr["right"]]
        return " | ".join(f"({jani_expression_to_text(item)})" for item in (items or []))
    if op in {"=", "≠", "!=", "<", "<=", ">", ">="}:
        left = jani_expression_to_text(expr["left"])
        right = jani_expression_to_text(expr["right"])
        return f"{left} {'!=' if op == '≠' else op} {right}"
    raise ValueError(f"unsupported JANI expression op {op!r}")


def jani_property(path: Path, name: str) -> dict[str, str]:
    data = load_json(path)
    item = next((p for p in data.get("properties", []) if p.get("name") == name), None)
    if item is None:
        raise ValueError(f"property {name!r} is not in {path}")

    expression = item.get("expression", {})
    if expression.get("op") == "filter":
        expression = expression.get("values", {})
    objective = expression.get("op")
    if objective in {"Emin", "Emax"}:
        target = jani_expression_to_text(expression["reach"])
        reward = expression.get("accumulate", [None])[0]
        if not reward:
            raise ValueError(f"JANI expected-reward property has no reward model: {expression!r}")
        return {
            "kind": "expected-reward",
            "objective": objective[1:].lower(),
            "formula": f'R{{"{reward}"}}{objective[1:].lower()}=? [F {target}]',
            "reward": reward,
            "target": target,
        }
    if objective not in {"Pmin", "Pmax"}:
        raise ValueError(f"unsupported JANI property {expression!r}")
    path_expr = expression.get("exp", {})
    if path_expr.get("op") == "F":
        target = jani_expression_to_text(path_expr.get("exp"))
        return {"kind": "reachability-probability", "objective": objective[1:].lower(), "formula": f"{objective}=? [F {target}]", "target": target}
    if path_expr.get("op") == "U":
        target = jani_expression_to_text(path_expr.get("right"))
        safe_expr = path_expr.get("left")
        if safe_expr is True:
            return {"kind": "reachability-probability", "objective": objective[1:].lower(), "formula": f"{objective}=? [F {target}]", "target": target}
        safe = jani_expression_to_text(safe_expr)
        return {"kind": "reachability-probability", "objective": objective[1:].lower(), "formula": f"{objective}=? [{safe} U {target}]", "safe": safe, "target": target}
    raise ValueError(f"unsupported JANI property {expression!r}")


def supported_property(instance: dict[str, Any]) -> dict[str, str]:
    if not instance["type"].startswith("mdp"):
        raise ValueError(f"model type is {instance['type']}, not mdp")

    if instance["program"] is None:
        if instance.get("jani") is None:
            raise ValueError("missing PRISM program or JANI model")
        return jani_property(instance["jani"], instance["property"])
    if instance["properties"] is None:
        raise ValueError("missing PRISM property file")

    formula = property_map(instance["properties"]).get(instance["property"])
    if formula is None:
        raise ValueError(f"property {instance['property']!r} is not in {instance['properties']}")

    reward = re.fullmatch(r'R\{"([^"]+)"\}(min|max)=\?\s*\[\s*F\s+(.+?)\s*\]', formula)
    if reward is not None:
        return {"kind": "expected-reward", "objective": reward.group(2), "formula": formula, "reward": reward.group(1), "target": reward.group(3)}

    steps = re.fullmatch(r'T(min|max)=\?\s*\[\s*F\s+(.+?)\s*\]', formula)
    if steps is not None:
        return {"kind": "expected-reward", "objective": steps.group(1), "formula": formula, "steps": True, "target": steps.group(2)}

    reach = re.fullmatch(r'P(min|max)=\?\s*\[\s*F\s+(.+?)\s*\]', formula)
    if reach is not None:
        return {"kind": "reachability-probability", "objective": reach.group(1), "formula": formula, "target": reach.group(2)}

    until = re.fullmatch(r'P(min|max)=\?\s*\[\s*(.+?)\s+U\s+(.+?)\s*\]', formula)
    if until is not None:
        return {"kind": "reachability-probability", "objective": until.group(1), "formula": formula, "safe": until.group(2), "target": until.group(3)}

    raise ValueError(f"unsupported property formula: {formula!r}")


def define_constants(stormpy: Any, program: Any, constants: str) -> Any:
    definitions = stormpy.parse_constants_string(program.expression_manager, constants)
    return program.define_constants(definitions)


def defined_constants(program: Any) -> dict[str, Any]:
    """The values of the constants of a PRISM program, including those fixed by the file.

    Target expressions may refer to them, e.g. N of consensus.4.prism. JANI constants
    do not expose their values, so only the index constants are known for JANI models.
    """
    if not hasattr(program, "get_undefined_constants"):
        return {}
    values = {}
    for constant in program.substitute_constants().constants:
        if not constant.defined:
            continue
        if constant.type.is_boolean:
            values[constant.name] = constant.definition.evaluate_as_bool()
        elif constant.type.is_integer:
            values[constant.name] = constant.definition.evaluate_as_int()
        else:
            values[constant.name] = constant.definition.evaluate_as_double()
    return values


def build_model(stormpy: Any, instance: dict[str, Any]) -> Any:
    """Build the nominal model; adds the constants of the model to instance["constants"]."""
    if instance["program"] is not None:
        program = stormpy.parse_prism_program(str(instance["program"]))
    else:
        program, _properties = stormpy.parse_jani_model(str(instance["jani"]))
    constants = ",".join(f"{name}={value_string(value)}" for name, value in instance["constants"].items())
    if constants:
        program = define_constants(stormpy, program, constants)
    instance["constants"] = {**defined_constants(program), **instance["constants"]}

    options = stormpy.BuilderOptions()
    options.set_build_state_valuations()
    options.set_build_all_labels()
    options.set_build_all_reward_models()
    return stormpy.build_sparse_model_with_options(program, options)


def bitvector_has(bitvector: Any, state: int) -> bool:
    try:
        return bool(bitvector.get(state))
    except AttributeError:
        return bool(bitvector[state])


def label_exists(model: Any, label: str) -> bool:
    if hasattr(model.labeling, "contains_label"):
        return bool(model.labeling.contains_label(label))
    try:
        model.labeling.get_states(label)
        return True
    except Exception:
        return False


def label_states(model: Any, label: str) -> set[int]:
    states = model.labeling.get_states(label)
    return {state for state in range(model.nr_states) if bitvector_has(states, state)}


def valuations(model: Any) -> list[dict[str, Any]]:
    if not hasattr(model, "has_state_valuations") or not model.has_state_valuations():
        return [{} for _ in range(model.nr_states)]
    return [json.loads(str(model.state_valuations.get_json(s))) for s in range(model.nr_states)]


def expression_to_python(expression: str) -> str:
    text = expression.strip()
    text = re.sub(r'"([^"]+)"', lambda m: f'__label({m.group(1)!r})', text)
    text = re.sub(r"(?<![<>=!])=(?![=])", "==", text)
    text = re.sub(r"!(?!=)", " not ", text)
    text = text.replace("&", " and ").replace("|", " or ")
    text = re.sub(r"\btrue\b", "True", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfalse\b", "False", text, flags=re.IGNORECASE)
    return text


def states_satisfying(model: Any, expression: str, constants: dict[str, Any] | None = None) -> set[int]:
    expression = expression.strip()

    quoted_label = re.fullmatch(r'"([^"]+)"', expression)
    if quoted_label:
        return label_states(model, quoted_label.group(1))
    bare_label = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expression)
    if bare_label and label_exists(model, expression):
        return label_states(model, expression)

    label_cache: dict[str, set[int]] = {}

    def has_label(name: str, state: int) -> bool:
        if name not in label_cache:
            label_cache[name] = label_states(model, name)
        return state in label_cache[name]

    def eval_with_env(env_for_state: Any) -> set[int]:
        code = compile(expression_to_python(expression), "<property expression>", "eval")
        result: set[int] = set()
        for state in range(model.nr_states):
            env = env_for_state(state)
            env["__label"] = lambda name, state=state: has_label(name, state)
            try:
                if bool(eval(code, {"__builtins__": {}}, env)):
                    result.add(state)
            except NameError as exc:
                raise ValueError(f"unsupported property expression {expression!r}: {exc}") from exc
        return result

    unquoted = re.sub(r'"[^"]+"', "", expression)
    identifiers = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", unquoted))
    identifiers -= {"true", "false", "True", "False"}
    if not identifiers:
        return eval_with_env(lambda _state: {})

    state_values = valuations(model)
    constants = constants or {}
    return eval_with_env(lambda state: dict(constants, **state_values[state]))


def row_distribution(model: Any, row: int) -> tuple[list[int], list[float]]:
    """Return the 1-indexed support and probabilities for one transition row.

    stormpy sparse rows are already stored by column and do not contain duplicate
    columns, so avoid the expensive dict aggregation/sort that used to dominate
    benchmark generation time.
    """
    support: list[int] = []
    values: list[float] = []
    total = 0.0

    for entry in model.transition_matrix.get_row(row):
        probability = float(entry.value())
        if probability <= 0.0:
            continue
        support.append(int(entry.column) + 1)
        values.append(probability)
        total += probability

    if not math.isclose(total, 1.0, rel_tol=1e-8, abs_tol=1e-10):
        raise ValueError(f"transition row {row} sums to {total}")
    if total != 1.0:
        inv_total = 1.0 / total
        values = [p * inv_total for p in values]
    return support, values


def reward_vectors(model: Any, reward_name: str) -> tuple[list[float], list[float]]:
    rewards = model.reward_models[reward_name]
    state = [0.0] * model.nr_states
    choice = [0.0] * model.nr_choices
    if getattr(rewards, "has_state_rewards", False):
        state = [float(x) for x in rewards.state_rewards]
    if getattr(rewards, "has_state_action_rewards", False):
        choice = [float(x) for x in rewards.state_action_rewards]
    return state, choice


def prepare_conversion(model: Any, instance: dict[str, Any], prop: dict[str, str]) -> dict[str, Any]:
    """Extract once; all exporters use the same zero-based states and choices."""
    if len(model.initial_states) != 1:
        raise ValueError(f"expected one initial state, got {model.initial_states}")
    targets = states_satisfying(model, prop["target"], instance["constants"])
    avoid = set()
    if "safe" in prop:
        safe = states_satisfying(model, prop["safe"], instance["constants"])
        avoid = set(range(model.nr_states)) - safe - targets
    reward = prop["kind"] == "expected-reward"
    if reward:
        if prop.get("steps"):
            state_rewards = [1.0] * model.nr_states
            choice_rewards = [0.0] * model.nr_choices
        else:
            rm = model.reward_models[prop["reward"]]
            if getattr(rm, "has_transition_rewards", False):
                raise ValueError("transition-dependent rewards are not supported by this exporter")
            state_rewards, choice_rewards = reward_vectors(model, prop["reward"])
    else:
        state_rewards = [0.0] * model.nr_states
        choice_rewards = [0.0] * model.nr_choices
    states = []
    # Matrix row of every exported choice; None marks a synthetic deadlock loop.
    rows = []
    deadlocks = label_states(model, "deadlock") if label_exists(model, "deadlock") else set()
    for state in range(model.nr_states):
        choices = []
        state_rows = list(model.transition_matrix.get_rows_for_group(state))
        for row in state_rows:
            support, probabilities = row_distribution(model, row)
            choices.append(([dest - 1 for dest in support], probabilities, choice_rewards[row]))
        if not choices:
            deadlocks.add(state)
            choices = [([state], [1.0], 0.0)]
            state_rows = [None]
        states.append(choices)
        rows.append(state_rows)
    # IntervalMDP.jl 0.7's importer requires a rectangular action space.
    # Duplicate an enabled action, never add a new behavior (e.g. a self-loop).
    num_actions = max(map(len, states))
    original_choices = sum(map(len, states))
    for choices, state_rows in zip(states, rows):
        choices.extend([choices[0]] * (num_actions - len(choices)))
        state_rows.extend([state_rows[0]] * (num_actions - len(state_rows)))
    return dict(states=states, rows=rows, initial_state=int(model.initial_states[0]),
                targets=targets, avoid=avoid, deadlocks=deadlocks,
                state_rewards=state_rewards, property=prop, source=instance,
                original_choices=original_choices, num_actions=num_actions)


def minimal_value(prepared: dict[str, Any], uncertainty: dict[str, Any]) -> float:
    """Storm's lower clamp, capped so every nominal probability p < 1 satisfies m <= p <= 1 - m.

    AddUncertainty rejects p < m and clips upper bounds to 1 - m, so a larger m
    would fail or exclude the nominal distribution.
    """
    smallest = min((min(p, 1 - p) for choices in prepared["states"]
                    for _, probabilities, _ in choices for p in probabilities if p < 1), default=1.0)
    return min(uncertainty["minimal_value"], smallest)


def add_uncertainty(stormpy: Any, model: Any, prepared: dict[str, Any],
                    uncertainty: dict[str, Any]) -> tuple[list[list[tuple[float, float]]], float]:
    """Interval bounds per matrix row from stormpy's AddUncertainty (absolute width)."""
    minimum = minimal_value(prepared, uncertainty)
    interval_model = stormpy.AddUncertaintyDouble(model).transform(uncertainty["delta"], minimum)
    matrix, nominal = interval_model.transition_matrix, model.transition_matrix
    bounds = []
    for row in range(interval_model.nr_choices):
        row_bounds = []
        # Same entries as row_distribution: drop explicitly stored zeros.
        for entry, original in zip(matrix.get_row(row), nominal.get_row(row)):
            p = float(original.value())
            if p <= 0.0:
                continue
            value = entry.value()
            row_bounds.append((float(value.lower()), float(value.upper())))
        # AddUncertainty only fixes p == 1 exactly; a deterministic row stored as
        # 0.9999999999999999 would get the empty interval [p, 1 - m].
        if len(row_bounds) == 1:
            row_bounds = [(1.0, 1.0)]
        bounds.append(row_bounds)
    return bounds, minimum


def check_bounds(probabilities: list[float], bounds: list[tuple[float, float]]) -> None:
    tolerance = 1e-12
    if any(not (0 < lo <= p + tolerance and p - tolerance <= hi <= 1) for p, (lo, hi) in zip(probabilities, bounds)):
        raise ValueError(f"interval bounds {bounds} do not contain nominal {probabilities}")
    if sum(lo for lo, _ in bounds) > 1 + tolerance or sum(hi for _, hi in bounds) < 1 - tolerance:
        raise ValueError(f"infeasible interval distribution {bounds}")


def write_explicit(prepared: dict[str, Any], uncertainty: dict[str, Any], base: Path,
                   bounds: list[list[tuple[float, float]]], minimum: float) -> None:
    """Stream the same intervals to Storm DRN and PRISM/IntervalMDP explicit files.

    bounds[row] holds the intervals of matrix row `row`, aligned with its support.
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    delta = uncertainty["delta"]
    states = prepared["states"]
    n = len(states)
    choices = sum(map(len, states))
    transitions = sum(len(support) for actions in states for support, _, _ in actions)
    reward = prepared["property"]["kind"] == "expected-reward"
    pathprop = '!"avoid" U "reach"' if "safe" in prepared["property"] else 'F "reach"'
    objective = prepared["property"].get("objective", "max")
    opposite_objective = "min" if objective == "max" else "max"
    policy = "maximize" if objective == "max" else "minimize"
    prism_property = f'{"R" if reward else "P"}{objective}{opposite_objective}=? [ {pathprop} ]'
    # DRN has no declaration for labels that hold in zero states.
    storm_pathprop = pathprop if prepared["avoid"] else 'F "reach"'
    if not prepared["targets"]:
        storm_pathprop = storm_pathprop.replace('"reach"', 'false')
    # Storm resolves nature through --uncertainty-resolution robust, not the property.
    storm_property = f'{"R" if reward else "P"}{objective}=? [ {storm_pathprop} ]'
    # Append extensions: benchmark IDs themselves contain dots.
    def path(extension: str) -> Path:
        return Path(str(base) + extension)
    # Regenerate every bundle so a changed width never silently reuses stale output.
    path(".txt").unlink(missing_ok=True)
    with ExitStack() as stack:
        def output(extension: str):
            return stack.enter_context(path(extension).open("w", encoding="utf-8"))
        drn, tra, sta, lab = (output(ext) for ext in (".drn", ".tra", ".sta", ".lab"))
        drn.write(f"@type: MDP\n@value_type: double-interval\n")
        if reward:
            drn.write("@reward_models\nreward\n")
        drn.write(f"@nr_states\n{n}\n@nr_choices\n{choices}\n@model\n")
        tra.write(f"{n} {choices} {transitions}\n")
        sta.write("(s)\n")
        labels = ["init", "deadlock", "reach", "avoid"]
        lab.write(" ".join(f'{i}="{label}"' for i, label in enumerate(labels)) + "\n")
        if reward:
            srew, trew = output(".srew"), output(".trew")
            nonzero_states = sum(r != 0 for r in prepared["state_rewards"])
            nonzero_transitions = sum(len(support) for actions in states for support, _, r in actions if r != 0)
            srew.write(f"{n} {nonzero_states}\n")
            trew.write(f"{n} {choices} {nonzero_transitions}\n")
        for state, actions in enumerate(states):
            state_labels = []
            for index, condition in enumerate((state == prepared["initial_state"],
                    state in prepared["deadlocks"], state in prepared["targets"], state in prepared["avoid"])):
                if condition:
                    state_labels.append(index)
            sta.write(f"{state}:({state})\n")
            if state_labels:
                lab.write(f"{state}: " + " ".join(map(str, state_labels)) + "\n")
            r = prepared["state_rewards"][state]
            state_reward = f" [{r:.17g}]" if reward else ""
            drn.write(f"state {state}{state_reward} " + " ".join(labels[i] for i in state_labels) + "\n")
            if reward and r != 0:
                srew.write(f"{state} {r:.17g}\n")
            for action, ((support, probabilities, action_reward), row) in enumerate(zip(actions, prepared["rows"][state])):
                action_rew = f" [{action_reward:.17g}]" if reward else ""
                drn.write(f"  action {action}{action_rew}\n")
                if reward and action_reward != 0:
                    # PRISM repeats each state/action reward on every successor.
                    for dest in support:
                        trew.write(f"{state} {action} {dest} {action_reward:.17g}\n")
                row_bounds = [(1.0, 1.0)] if row is None else bounds[row]
                check_bounds(probabilities, row_bounds)
                for dest, (lower, upper) in zip(support, row_bounds):
                    value = f"[{lower:.17g},{upper:.17g}]"
                    drn.write(f"    {dest} : {value}\n")
                    tra.write(f"{state} {action} {dest} {value}\n")
    if not reward:
        for ext in (".srew", ".trew"):
            path(ext).unlink(missing_ok=True)
    path(".pctl").write_text(prism_property + "\n", encoding="utf-8")
    path(".storm.props").write_text(storm_property + "\n", encoding="utf-8")
    source = prepared["source"]
    compatibility = ("unsupported: IntervalMDP.jl cannot import reachability rewards"
                     if reward else "supported")
    path(".txt").write_text(
        f"benchmark: {source['id']}\nsource: {source['program'] or source['jani']}\n"
        f"constants: {source['constants_string']}\noriginal_property: {prepared['property']['formula']}\n"
        f"uncertainty: absolute (stormpy AddUncertainty), delta={delta}, minimal_value={minimum:.17g}\n"
        f"semantics: {policy} policy, adversarial ({opposite_objective}imize) uncertainty; deterministic rows unchanged\n"
        f"states: {n}\noriginal_choices: {prepared['original_choices']}\nexported_choices: {choices}\n"
        f"padding: duplicate first enabled action\nIntervalMDP.jl: {compatibility}\n"
        "Storm: use .drn, .storm.props and --uncertainty-resolution robust\n"
        "PRISM: import .tra/.sta/.lab as imdp; query .pctl; import .srew/.trew for rewards\n",
        encoding="utf-8")


def is_min_reward(prop: dict[str, str]) -> bool:
    """Whether a property minimises an expected reward; Storm cannot be trusted on these."""
    return prop["kind"] == "expected-reward" and prop["objective"] == "min"


def write_index(output_dir: Path, uncertainties: list[dict[str, Any]], benchmark_root: Path,
                skip_min_rewards: bool = False) -> int:
    """Write output_dir/index.json with one entry per complete bundle and interval configuration.

    The entries are what common/generate_invocations.py and common/run.py expect: the
    bundle prefix relative to output_dir and the values the commands in
    scripts/configurations.json refer to. The type is the interval configuration, so
    that the plots can tell them apart. Nominal bundles inherit the reference result
    of their benchmark. With skip_min_rewards, bundles of minimising reward properties
    are left out. Returns the number of entries.
    """
    shared = load_json(benchmark_root / "index.json")
    index = {}
    for uncertainty in uncertainties:
        folder = output_dir / uncertainty["id"]
        # The .txt metadata is written last, so it marks a complete bundle.
        for marker in sorted(folder.glob("*.txt")):
            base = marker.name[:-len(".txt")]
            if skip_min_rewards and (folder / f"{base}.pctl").read_text(encoding="utf-8").startswith("Rmin"):
                continue
            metadata = dict(line.split(": ", 1) for line in marker.read_text(encoding="utf-8").splitlines())
            benchmark_id = metadata["benchmark"]
            rewards = (folder / f"{base}.srew").exists()
            entry = {
                "type": uncertainty["id"],
                "benchmark": benchmark_id,
                "delta": uncertainty["delta"],
                "bundle": f"{uncertainty['id']}/{base}",
                "prism-import": "tra,sta,lab,srew,trew" if rewards else "tra,sta,lab",
                "intervalmdp": metadata["IntervalMDP.jl"] == "supported",
                "states": int(metadata["states"]),
            }
            reference = shared.get(benchmark_id, {}).get("reference-result")
            if uncertainty["delta"] == 0 and reference is not None:
                entry["reference-result"] = reference
            index[f"{benchmark_id}.{uncertainty['id']}"] = entry
    with (output_dir / "index.json").open("w", encoding="utf-8") as handle:
        json.dump(index, handle, indent="\t")
        handle.write("\n")
    return len(index)


def load_uncertainties(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    specs = data["sets"] if isinstance(data, dict) else data
    if not specs:
        raise ValueError("at least one interval configuration is required")
    ids = set()
    for spec in specs:
        if spec.get("type") != "absolute" or "delta" not in spec:
            raise ValueError("use interval configurations with type='absolute' and delta; legacy relative widths and uncertainty balls are unsupported")
        spec["delta"] = float(spec["delta"])
        spec["minimal_value"] = float(spec.get("minimal_value", 1e-4))
        if not math.isfinite(spec["delta"]) or not 0 <= spec["delta"] < 1:
            raise ValueError("delta must be finite and satisfy 0 <= delta < 1")
        if not 0 < spec["minimal_value"] <= 0.5:
            raise ValueError("minimal_value must satisfy 0 < minimal_value <= 0.5")
        name = spec.get("id")
        if not isinstance(name, str) or not name or slug(name) != name or name in {".", ".."} or name in ids:
            raise ValueError(f"invalid or duplicate configuration id: {name!r}")
        ids.add(name)
    return specs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK_ROOT)
    parser.add_argument("--benchmark-set", type=Path, default=DEFAULT_BENCHMARK_SET)
    parser.add_argument("--uncertainty-config", type=Path, default=DEFAULT_UNCERTAINTY_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--benchmark", action="append")
    parser.add_argument("--max-states", type=int, default=DEFAULT_MAX_STATES, help=f"Skip generated models with more than this many states (default: {DEFAULT_MAX_STATES}); use 0 to disable.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve benchmarks and list outputs without importing Stormpy or building models.")
    parser.add_argument("--workers", type=int, default=1, help="Number of benchmark conversions to run in parallel.")
    parser.add_argument("--keep-going", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-min-rewards", action=argparse.BooleanOptionalAction, default=True,
                        help="Leave out benchmarks minimising an expected reward (Rmin), which Storm "
                             "does not support for interval models (default: on).")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def process_benchmark(task: dict[str, Any]) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    benchmark_id = task["benchmark_id"]
    benchmark_root = task["benchmark_root"]
    output_dir = task["output_dir"]
    uncertainties = task["uncertainties"]
    dry_run = task["dry_run"]
    max_states = task["max_states"]
    log = [f"Generating {benchmark_id}"]
    print(f"Started {benchmark_id}", flush=True)
    manifest: dict[str, list[dict[str, Any]]] = {"created": [], "skipped": []}

    try:
        instance = resolve_benchmark(benchmark_root, benchmark_id)
        prop = supported_property(instance)
        benchmark_slug = slug(benchmark_id)
        outputs = [
            (uncertainty, output_dir / slug(uncertainty["id"]) / benchmark_slug)
            for uncertainty in uncertainties
        ]
        if dry_run:
            for uncertainty, out in outputs:
                manifest["created"].append({
                    "benchmark_id": benchmark_id,
                    "uncertainty_set": uncertainty["id"],
                    "path": str(out),
                })
            return log, manifest

        import stormpy

        model = build_model(stormpy, instance)
        log.append(f"{model.nr_states} states")
        if max_states > 0 and model.nr_states > max_states:
            manifest["skipped"].append({
                "benchmark_id": benchmark_id,
                "reason": f"too many states ({model.nr_states:,} > {max_states:,})",
            })
            return log, manifest

        prepared = prepare_conversion(model, instance, prop)
        print(prepared["property"])
        for uncertainty, out in outputs:
            bounds, minimum = add_uncertainty(stormpy, model, prepared, uncertainty)
            write_explicit(prepared, uncertainty, out, bounds, minimum)
            del bounds
            manifest["created"].append({"benchmark_id": benchmark_id, "uncertainty_set": uncertainty["id"], "path": str(out)})
    except Exception as exc:
        log.append(str(exc))
        manifest["skipped"].append({"benchmark_id": benchmark_id, "reason": str(exc)})
        if not task["keep_going"]:
            raise

    return log, manifest


def min_reward_benchmark(benchmark_root: Path, benchmark_id: str) -> bool:
    """Whether the benchmark minimises an expected reward; unresolvable ones are kept, to fail later."""
    try:
        return is_min_reward(supported_property(resolve_benchmark(benchmark_root, benchmark_id)))
    except (LookupError, ValueError):
        return False


def main() -> int:
    args = parse_args()
    benchmark_ids = load_json(args.benchmark_set)
    if args.benchmark:
        wanted = set(args.benchmark)
        benchmark_ids = [b for b in benchmark_ids if b in wanted]
    if args.skip_min_rewards:
        benchmark_ids = [b for b in benchmark_ids if not min_reward_benchmark(args.benchmark_root, b)]

    uncertainties = load_uncertainties(args.uncertainty_config)
    manifest: dict[str, Any] = {"created": [], "skipped": []}
    tasks = [
        {
            "benchmark_id": benchmark_id,
            "benchmark_root": args.benchmark_root,
            "output_dir": args.output_dir,
            "uncertainties": uncertainties,
            "dry_run": args.dry_run,
            "max_states": args.max_states,
            "keep_going": args.keep_going,
        }
        for benchmark_id in benchmark_ids
    ]

    def record_results(results: Any) -> None:
        for log, partial in results:
            for line in log:
                print(line)
            manifest["created"].extend(partial["created"])
            manifest["skipped"].extend(partial["skipped"])

    if not tasks:
        pass
    elif args.workers == 1:
        record_results(map(process_benchmark, tasks))
    else:
        worker_count = min(args.workers, len(tasks))
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=worker_count,
        ) as executor:
            record_results(executor.map(process_benchmark, tasks))

    if not args.dry_run:
        entries = write_index(args.output_dir, uncertainties, args.benchmark_root, args.skip_min_rewards)
        print(f"Wrote {entries} entries to {args.output_dir / 'index.json'}")
    verb = "Would create" if args.dry_run else "Created"
    print(f"{verb} {len(manifest['created'])} bundles; skipped {len(manifest['skipped'])}.")
    for item in manifest["skipped"][:10]:
        print(f"  {item['benchmark_id']}: {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
