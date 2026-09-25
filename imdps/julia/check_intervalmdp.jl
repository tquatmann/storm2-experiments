# Install IntervalMDP 0.7 in your Julia environment before running this script.
using IntervalMDP
using IntervalMDP.Data

length(ARGS) == 1 || error("Usage: julia --project=ENV check_intervalmdp.jl PATH_WITHOUT_EXTENSION")
base = only(ARGS)
property = strip(read(base * ".pctl", String))
# startswith(property, "Pmaxmin") || error(
#     "This runner supports reachability/reach-avoid exports. " *
#     "IntervalMDP.jl's importer does not support reachability-reward queries; use Storm or PRISM for those.",
# )
println("setup done")
# Julia's startup and compilation dominate small models, so time the steps themselves.
read_time = @elapsed (problem = read_prism_file(base))
println("read file")
solve_time = @elapsed (solution = solve(problem))
println("solved it!")
values = value_function(solution)
for state in initial_states(system(problem))
    println("Initial state ", state - 1, ": ", values[state])
end
println("Iterations: ", num_iterations(solution))
println("Maximum Bellman residual: ", maximum(abs, residual(solution)))
println("Time for reading: ", read_time, "s")
println("Time for solving: ", solve_time, "s")
