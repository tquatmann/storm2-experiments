# Install IntervalMDP 0.7 in your Julia environment before running this script.
using IntervalMDP
using IntervalMDP.Data

usage = "Usage: julia --project=ENV check_intervalmdp.jl [--precision EPS] PATH_WITHOUT_EXTENSION"
# Value iteration stops once the largest change of a value is below the precision (absolute).
tolerance = 1e-6
if length(ARGS) == 3 && ARGS[1] == "--precision"
    tolerance = parse(Float64, ARGS[2])
elseif length(ARGS) != 1
    error(usage)
end
base = last(ARGS)
property = strip(read(base * ".pctl", String))
# startswith(property, "Pmaxmin") || error(
#     "This runner supports reachability/reach-avoid exports. " *
#     "IntervalMDP.jl's importer does not support reachability-reward queries; use Storm or PRISM for those.",
# )

# IntervalMDP.jl's importer cannot read reachability rewards. For rewards that count the
# steps until "reach", the generator writes BASE.intervalmdp.pctl with Tmaxmin=? [ F "reach" ]
# (expected number of steps), which is IntervalMDP.jl's ExpectedExitTime.
function read_exit_time_problem(base, precision)
    property = strip(read(base * ".intervalmdp.pctl", String))
    m = match(r"^T(?<strategy>max|min)(?<adversary>max|min)=\? \[ F \"reach\" \]$", property)
    isnothing(m) && error("Unsupported property $property")
    num_states = Data.read_prism_states_file(base * ".sta")
    probs, num_actions = Data.read_prism_transitions_file(base * ".tra", num_states)
    labels = Data.read_prism_labels(base * ".lab")
    prop = ExpectedExitTime(Data.find_states_label(labels, "reach"), precision)
    strategy_mode = m[:strategy] == "max" ? Maximize : Minimize
    satisfaction_mode = m[:adversary] == "min" ? Pessimistic : Optimistic
    mdp = IntervalMarkovDecisionProcess(probs, num_actions, Data.find_initial_states(labels))
    return ControlSynthesisProblem(mdp, Specification(prop, satisfaction_mode, strategy_mode))
end

# The importer always uses a precision of 1e-6 for unbounded properties; replace it.
function with_precision(problem, precision)
    spec = specification(problem)
    prop = system_property(spec)
    if prop isa InfiniteTimeReachability
        prop = InfiniteTimeReachability(reach(prop), precision)
    elseif prop isa InfiniteTimeReachAvoid
        prop = InfiniteTimeReachAvoid(reach(prop), avoid(prop), precision)
    end
    spec = Specification(prop, satisfaction_mode(spec), strategy_mode(spec))
    return ControlSynthesisProblem(system(problem), spec)
end

println("setup done")
# Julia's startup and compilation dominate small models, so time the steps themselves.
read_time = @elapsed (problem = isfile(base * ".intervalmdp.pctl") ? read_exit_time_problem(base, tolerance) :
                                with_precision(read_prism_file(base), tolerance))
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
