# Build intervalmdp.so, a sysimage containing IntervalMDP.jl with the code that
# check_intervalmdp.jl needs already compiled (see precompile.jl). scripts/intervalmdp.sh
# uses it if present, so that the runs do not measure Julia compiling the solver.
#
# Run from imdps/ with
#   bin/julia julia/build_sysimage.jl
# PackageCompiler.jl is installed into a temporary environment, not into julia/.
import Pkg
here = @__DIR__
Pkg.activate(; temp=true)
Pkg.add(name="PackageCompiler", version="2")
using PackageCompiler

create_sysimage(["IntervalMDP"];
                project=here,
                sysimage_path=joinpath(here, "intervalmdp.so"),
                precompile_execution_file=joinpath(here, "precompile.jl"))
