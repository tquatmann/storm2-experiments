#!/bin/sh
# Run julia/check_intervalmdp.jl on a bundle, with the julia linked into bin/ and
# the IntervalMDP.jl environment in julia/. Usage: intervalmdp.sh PATH_WITHOUT_EXTENSION
# If julia/intervalmdp.so exists (see julia/build_sysimage.jl), it is used as the
# sysimage, so that the solver does not have to be compiled on every run.
root=$(cd "$(dirname "$0")/.." && pwd)
sysimage=""
[ -f "$root/julia/intervalmdp.so" ] && sysimage="--sysimage=$root/julia/intervalmdp.so"
exec "$root/bin/julia" $sysimage --project="$root/julia" "$root/julia/check_intervalmdp.jl" "$@"
