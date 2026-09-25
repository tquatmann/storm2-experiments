#!/bin/sh
# Run julia/check_intervalmdp.jl on a bundle, with the julia linked into bin/ and
# the IntervalMDP.jl environment in julia/. Usage: intervalmdp.sh PATH_WITHOUT_EXTENSION
root=$(cd "$(dirname "$0")/.." && pwd)
exec "$root/bin/julia" --project="$root/julia" "$root/julia/check_intervalmdp.jl" "$@"
