import json
import math
from pathlib import Path
import tempfile
import unittest

import generate_explicit_benchmarks as g


class Exports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / 'example.1.goal'
        self.prepared = dict(
            states=[ [([1, 2], [.5, .5], 2.0), ([1, 2], [.3, .7], 0.0)],
                     [([1], [1.0], 0.0)] * 2, [([2], [1.0], 0.0)] * 2 ],
            initial_state=0, targets={1}, avoid={2}, deadlocks=set(),
            rows=[[0, 1], [2, 3], [4, 5]],
            state_rewards=[1., 0., 0.], original_choices=4, num_actions=2,
            property=dict(kind='reachability-probability', formula='Pmax=? [F goal]'),
            source=dict(id='example.1.goal', program='example.pm', jani=None, constants_string=''),
        )

    def export(self, delta=.05, minimum=1e-4):
        # Mirror stormpy's AddUncertainty on the fixture's rows.
        def interval(p):
            return (1., 1.) if p == 1 else (max(p - delta, minimum), min(p + delta, 1 - minimum))
        bounds = [[interval(p) for p in probabilities]
                  for choices in self.prepared['states'] for _, probabilities, _ in choices]
        g.write_explicit(self.prepared, dict(id='test', type='absolute', delta=delta), self.base, bounds, minimum)

    def read(self, suffix):
        return Path(str(self.base) + suffix).read_text()

    def test_add_uncertainty(self):
        try:
            import stormpy
        except ImportError:
            self.skipTest('install stormpy for Storm integration')
        model_file = Path(self.tmp.name) / 'tiny.pm'
        model_file.write_text('''mdp
module m
 s : [0..3] init 0;
 [] s=0 -> 0.00001:(s'=1) + 0.49999:(s'=2) + 0.5:(s'=3);
 [] s>0 -> (s'=s);
endmodule
''')
        instance = dict(program=model_file, constants={}, id='tiny', jani=None)
        model = g.build_model(stormpy, instance)
        prepared = g.prepare_conversion(model, instance, dict(kind='reachability-probability', target='s=1'))
        for delta in [0, .01, .5]:
            spec = dict(id='x', type='absolute', delta=delta, minimal_value=1e-4)
            bounds, minimum = g.add_uncertainty(stormpy, model, prepared, spec)
            # Storm's default 1e-4 would reject the 1e-5 edge; it is capped instead.
            self.assertEqual(minimum, 1e-5)
            _, probabilities, _ = prepared['states'][0][0]
            g.check_bounds(probabilities, bounds[0])
            self.assertEqual(bounds[1], [(1., 1.)])
            for p, (lo, hi) in zip(probabilities, bounds[0]):
                self.assertAlmostEqual(lo, max(p - delta, minimum))
                self.assertAlmostEqual(hi, min(p + delta, 1 - minimum))
        for bad in [dict(delta=-.1), dict(delta=1), dict(delta=math.nan), dict(delta=.1, minimal_value=0)]:
            path = Path(self.tmp.name) / 'bad.json'
            path.write_text(json.dumps({'sets': [dict(id='bad', type='absolute', **bad)]}))
            with self.assertRaises(ValueError):
                g.load_uncertainties(path)

    def test_formats_share_intervals_and_counts(self):
        self.export()
        rows = self.read('.tra').splitlines()
        self.assertEqual(rows[0], '3 6 8')
        self.assertEqual(len(rows) - 1, 8)
        prism_values = [row.split()[3] for row in rows[1:]]
        drn_values = [row.split(' : ')[1] for row in self.read('.drn').splitlines() if ' : ' in row]
        self.assertEqual(prism_values, drn_values)
        self.assertEqual(self.read('.pctl'), 'Pmaxmin=? [ F "reach" ]\n')
        self.assertEqual(self.read('.storm.props'), 'Pmax=? [ F "reach" ]\n')
        self.assertEqual(len(self.read('.sta').splitlines()), 4)
        self.export(.5)
        self.assertNotEqual(self.read('.tra').splitlines()[1], rows[1])

    def test_reach_avoid_and_overlapping_initial_target(self):
        self.prepared['property']['safe'] = 'safe'
        self.prepared['targets'].add(0)
        self.export()
        self.assertIn('0: 0 2\n', self.read('.lab'))
        self.assertIn('!"avoid" U "reach"', self.read('.pctl'))

    def test_minimum_objective_is_preserved(self):
        self.prepared['property']['objective'] = 'min'
        self.export()
        self.assertEqual(self.read('.pctl'), 'Pminmax=? [ F "reach" ]\n')
        self.assertEqual(self.read('.storm.props'), 'Pmin=? [ F "reach" ]\n')

    def test_reward_encoding_and_compatibility(self):
        self.prepared['property']['kind'] = 'expected-reward'
        self.export()
        self.assertEqual(self.read('.srew'), '3 1\n0 1\n')
        self.assertEqual(self.read('.trew'), '3 6 2\n0 0 1 2\n0 0 2 2\n')
        self.assertIn('Rmaxmin=?', self.read('.pctl'))
        self.assertIn('IntervalMDP.jl: unsupported', self.read('.txt'))
        self.prepared['property']['kind'] = 'reachability-probability'
        self.export()
        self.assertFalse(Path(str(self.base) + '.trew').exists())

    def test_index(self):
        root = Path(self.tmp.name) / 'out'
        self.base = root / 'nominal' / 'prob'
        self.export(0)
        self.base = root / 'nominal' / 'rew'
        self.prepared['property']['kind'] = 'expected-reward'
        self.export(0)
        shared = Path(self.tmp.name) / 'shared'
        shared.mkdir()
        (shared / 'index.json').write_text(json.dumps({'example.1.goal': {'reference-result': '1/2'}}))
        uncertainties = [dict(id='nominal', delta=0.), dict(id='missing', delta=.1)]
        self.assertEqual(g.write_index(root, uncertainties, shared), 1)
        index = json.loads((root / 'index.json').read_text())
        # Both bundles belong to the same benchmark here, so the later one wins.
        entry = index['example.1.goal.nominal']
        self.assertEqual(entry['bundle'], 'nominal/rew')
        self.assertEqual(entry['prism-import'], 'tra,sta,lab,srew,trew')
        self.assertFalse(entry['intervalmdp'])
        self.assertEqual(entry['reference-result'], '1/2')
        self.assertEqual(entry['states'], 3)
        # The reward bundle maximises, so it is kept; a minimising one is left out.
        self.assertEqual(g.write_index(root, uncertainties, shared, skip_min_rewards=True), 1)
        self.prepared['property']['objective'] = 'min'
        self.export(0)
        self.assertEqual(g.write_index(root, uncertainties, shared, skip_min_rewards=True), 1)
        self.assertEqual(json.loads((root / 'index.json').read_text())['example.1.goal.nominal']['bundle'], 'nominal/prob')

    def test_min_reward_benchmarks(self):
        root = g.DEFAULT_BENCHMARK_ROOT
        self.assertTrue(g.min_reward_benchmark(root, 'csma.4-2.time_min'))
        self.assertFalse(g.min_reward_benchmark(root, 'consensus.6-2.steps_max'))
        self.assertFalse(g.min_reward_benchmark(root, 'consensus.4-4.disagree'))

    def test_resolve_shared_index(self):
        instance = g.resolve_benchmark(g.DEFAULT_BENCHMARK_ROOT, 'consensus.4-4.disagree')
        self.assertEqual(instance['constants'], {'K': 4})
        self.assertEqual(instance['program'].name, 'consensus.4.prism')
        self.assertEqual(g.supported_property(instance)['target'], '"finished"&!"agree"')
        with self.assertRaises(LookupError):
            g.resolve_benchmark(g.DEFAULT_BENCHMARK_ROOT, 'consensus.4-4.unknown')

    def test_storm_solves_export(self):
        try:
            import stormpy
        except ImportError:
            self.skipTest('install stormpy for Storm integration')
        self.export()
        model = stormpy.build_interval_model_from_drn(str(self.base) + '.drn')
        prop = stormpy.parse_properties(self.read('.storm.props'))[0]
        task = stormpy.CheckTask(prop.raw_formula)
        task.set_uncertainty_resolution_mode(stormpy.UncertaintyResolutionMode.ROBUST)
        result = stormpy.check_interval_mdp(model, task, stormpy.Environment())
        self.assertAlmostEqual(result.at(0), .45, places=7)
        self.export(0)
        model = stormpy.build_interval_model_from_drn(str(self.base) + '.drn')
        self.assertAlmostEqual(stormpy.check_interval_mdp(model, task, stormpy.Environment()).at(0), .5, places=7)

    def test_empty_target_and_avoid_sets(self):
        self.prepared['targets'] = set()
        self.prepared['avoid'] = set()
        self.prepared['property']['safe'] = 'true'
        self.export()
        self.assertEqual(self.read('.storm.props'), 'Pmax=? [ F false ]\n')
        self.assertEqual(self.read('.pctl'), 'Pmaxmin=? [ !"avoid" U "reach" ]\n')
        try:
            import stormpy
        except ImportError:
            return
        model = stormpy.build_interval_model_from_drn(str(self.base) + '.drn')
        prop = stormpy.parse_properties(self.read('.storm.props'))[0]
        task = stormpy.CheckTask(prop.raw_formula)
        task.set_uncertainty_resolution_mode(stormpy.UncertaintyResolutionMode.ROBUST)
        self.assertEqual(stormpy.check_interval_mdp(model, task, stormpy.Environment()).at(0), 0)

    def test_legacy_configuration_is_rejected(self):
        path = Path(self.tmp.name) / 'legacy.json'
        path.write_text('{"sets": [{"id": "l1", "type": "lp", "radius_scale": 0.1}]}')
        with self.assertRaisesRegex(ValueError, 'legacy'):
            g.load_uncertainties(path)

    def test_prepare_padding_preserves_enabled_actions(self):
        try:
            import stormpy
        except ImportError:
            self.skipTest('install stormpy for Storm integration')
        model_file = Path(self.tmp.name) / 'model.pm'
        model_file.write_text('''mdp
module m
 s : [0..2] init 0;
 [] s=0 -> 0.5:(s'=1) + 0.5:(s'=2);
 [] s=0 -> 0.3:(s'=1) + 0.7:(s'=2);
 [] s=1 -> (s'=1);
 [] s=2 -> (s'=2);
endmodule
label "goal" = s=1;
''')
        instance = dict(program=model_file, constants={}, id='test', jani=None)
        model = g.build_model(stormpy, instance)
        prop = dict(kind='reachability-probability', formula='Pmax=? [F "goal"]', target='"goal"')
        prepared = g.prepare_conversion(model, instance, prop)
        self.assertEqual(prepared['original_choices'], 4)
        self.assertEqual([len(actions) for actions in prepared['states']], [2, 2, 2])
        self.assertEqual(prepared['rows'], [[0, 1], [2, 2], [3, 3]])
        for actions in prepared['states'][1:]:
            self.assertIs(actions[0], actions[1])


if __name__ == '__main__':
    unittest.main()
