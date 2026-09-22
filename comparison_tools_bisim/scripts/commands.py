#!/usr/bin/env python3
"""Handling of the command lines defined in configurations.json.

Shared by generate_invocations.py, which uses it to skip configuration/benchmark
combinations that cannot be run, and by run.py, which builds the actual command.
"""

import re

# Benchmark keys whose value is the path of a model file. Such files are copied
# into the temporary directory of an invocation.
FILE_KEYS = {"jani", "prism", "prism-property"}

# Configuration keys holding a command line, in the order in which they are tried.
# The first one whose model file placeholders the benchmark provides is used, so
# input specific variants (e.g. a "cmdprism") can simply be added here.
CMD_KEYS = ["cmd"]

PLACEHOLDER = re.compile(r"%([A-Za-z][A-Za-z0-9-]*)")


def placeholders(cmd):
    """The placeholder names occurring in a command line, in order of appearance."""
    result = []
    for name in PLACEHOLDER.findall(cmd):
        if name not in result:
            result.append(name)
    return result


def pick_command(config, benchmark):
    """The command line of the configuration that the benchmark provides all files for.

    Returns None if the configuration is not applicable to the benchmark, i.e. if it
    needs a model file (say a PRISM program) that the benchmark does not have.
    """
    for key in CMD_KEYS:
        if key not in config:
            continue
        needed = [p for p in placeholders(config[key]) if p in FILE_KEYS]
        if all(p in benchmark for p in needed):
            return config[key]
    return None
