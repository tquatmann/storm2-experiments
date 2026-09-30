# Workload for build_sysimage.jl: runs check_intervalmdp.jl on small bundles of every
# query shape the generator writes (maximising and minimising, reachability, reach-avoid and
# expected exit time), so that the sysimage contains the code these need already compiled.
mktempdir() do dir
    base = joinpath(dir, "model")
    write(base * ".sta", "(s)\n0:(0)\n1:(1)\n2:(2)\n3:(3)\n")
    write(base * ".tra", """
        4 8 10
        0 0 1 [0.4,0.6]
        0 0 2 [0.4,0.6]
        0 1 3 [0.2,0.3]
        0 1 0 [0.7,0.8]
        1 0 1 [1,1]
        1 1 1 [1,1]
        2 0 2 [1,1]
        2 1 2 [1,1]
        3 0 3 [1,1]
        3 1 3 [1,1]
        """)
    write(base * ".lab", "0=\"init\" 1=\"deadlock\" 2=\"reach\" 3=\"avoid\"\n0: 0\n1: 2\n2: 3\n")
    function check()
        empty!(ARGS)
        push!(ARGS, "--precision", "1e-6", base)
        include(joinpath(@__DIR__, "check_intervalmdp.jl"))
    end
    for query in ["Pmaxmin=? [ F \"reach\" ]", "Pminmax=? [ F \"reach\" ]",
                  "Pmaxmin=? [ !\"avoid\" U \"reach\" ]", "Pminmax=? [ !\"avoid\" U \"reach\" ]"]
        write(base * ".pctl", query * "\n")
        check()
    end
    # Rewards that count steps, solved as expected exit times; "reach" is reached almost surely.
    write(base * ".sta", "(s)\n0:(0)\n1:(1)\n")
    write(base * ".tra", """
        2 4 5
        0 0 0 [0.4,0.6]
        0 0 1 [0.4,0.6]
        0 1 1 [1,1]
        1 0 1 [1,1]
        1 1 1 [1,1]
        """)
    write(base * ".lab", "0=\"init\" 1=\"deadlock\" 2=\"reach\" 3=\"avoid\"\n0: 0\n1: 2\n")
    write(base * ".pctl", "Rmaxmin=? [ F \"reach\" ]\n")
    for query in ["Tmaxmin=? [ F \"reach\" ]", "Tminmax=? [ F \"reach\" ]"]
        write(base * ".intervalmdp.pctl", query * "\n")
        check()
    end
end
