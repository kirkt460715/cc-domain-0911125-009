import math
import random
import tempfile
import unittest
from pathlib import Path
from trialvault.service import Vault
from trialvault.validation import VaultError, canonical
from trialvault.interim.statistics import norm_cdf, norm_ppf
from trialvault.interim.spending import solve_boundaries


def specimen_schema(version=1):
    return {
        "id": "specimens",
        "version": version,
        "fields": [
            {"name": "batch", "type": "text"},
            {"name": "specimen", "type": "text"},
            {"name": "strength", "type": "decimal", "nullable": True},
        ],
        "primary_key": ["batch", "specimen"],
    }


def pilot_schema():
    return {
        "id": "pilot",
        "version": 1,
        "fields": [
            {"name": "id", "type": "text"},
            {"name": "reading", "type": "decimal"},
        ],
        "primary_key": ["id"],
    }


def jsonl(rows):
    return ("\n".join(canonical(row) for row in rows) + "\n").encode()


def specimen_rows(batches):
    rows = []
    for batch, values in batches:
        for index, value in enumerate(values, 1):
            rows.append(
                {"batch": batch, "specimen": f"{batch}-S{index}", "strength": value}
            )
    return rows


def design_kwargs(**overrides):
    schedule = [{"batch": f"A{i}", "group": "A"} for i in range(1, 5)] + [
        {"batch": f"B{i}", "group": "B"} for i in range(1, 5)
    ]
    kwargs = {
        "design_id": "study",
        "dataset": "trial",
        "schema_version": 1,
        "fields": {"batch": "batch", "specimen": "specimen", "value": "strength"},
        "schedule": schedule,
        "benefit_direction": "A_minus_B",
        "sidedness": "two",
        "alpha": "0.05",
        "max_batches_per_group": 4,
        "planned_fractions": ["0.5", "1"],
        "min_batches_per_group": 2,
        "specimens_per_batch": 2,
        "lock_rule": {
            "batch_lock": "dataset_version",
            "min_specimens_per_batch": 2,
            "missing_value": "exclude",
        },
        "scale": {
            "common_sd": "1.2",
            "source": {"dataset": "pilot-run", "version": 1},
            "note": "Frozen pilot scale",
        },
        "assumptions": [
            "Batch summaries approximately normal",
            "Batches independent",
            "Common SD frozen from pilot",
            "Equal allocation",
        ],
    }
    kwargs.update(overrides)
    return kwargs


class InterimTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Vault(Path(self.temp.name) / "vault")
        self.addCleanup(self.vault.close)
        self.vault.registry.add_schema(pilot_schema())
        self.vault.registry.register("pilot-run", "Pilot readings", "pilot", "operator")
        self.vault.ingestion.ingest(
            "pilot-v1",
            "pilot-run",
            1,
            jsonl([{"id": "p1", "reading": "1.1"}]),
            "jsonl",
            "internal",
            ["research"],
        )
        self.vault.registry.add_schema(specimen_schema())
        self.vault.registry.register("trial", "Batch trial", "specimens", "operator")

    def ingest_trial(self, request, batches, expected=0):
        return self.vault.ingestion.ingest(
            request,
            "trial",
            1,
            jsonl(specimen_rows(batches)),
            "jsonl",
            "internal",
            ["research"],
            expected_version=expected,
        )

    def register_design(self, **overrides):
        return self.vault.interim.register_design(**design_kwargs(**overrides))


class StatisticsTests(unittest.TestCase):
    def test_normal_functions(self):
        self.assertAlmostEqual(norm_cdf(1.959963984540054), 0.975, places=12)
        self.assertAlmostEqual(norm_ppf(0.975), 1.959963984540054, places=12)
        for p in (0.001, 0.01, 0.25, 0.5, 0.9, 0.999):
            self.assertAlmostEqual(norm_cdf(norm_ppf(p)), p, places=12)

    def test_first_look_matches_spending_identity(self):
        # OBF spending first look: c1 = z_{1-alpha/2} / sqrt(t1) exactly
        solution = solve_boundaries(0.05, "two", [1 / 3, 2 / 3, 1.0])
        self.assertEqual(solution["status"], "ok")
        expected = norm_ppf(0.975) / math.sqrt(1 / 3)
        self.assertAlmostEqual(solution["looks"][0]["boundary"], expected, places=9)
        self.assertAlmostEqual(solution["cumulative_achieved"], 0.05, places=9)
        for look in solution["looks"]:
            self.assertEqual(look["solver"]["status"], "ok")
            self.assertLess(look["solver"]["increment_error"], 1e-9)

    def test_single_look_recovers_fixed_sample_boundary(self):
        solution = solve_boundaries(0.05, "two", [1.0])
        self.assertAlmostEqual(
            solution["looks"][0]["boundary"], 1.959963984540054, places=9
        )
        one_sided = solve_boundaries(0.05, "one", [1.0])
        self.assertAlmostEqual(
            one_sided["looks"][0]["boundary"], norm_ppf(0.95), places=9
        )

    def test_one_sided_alpha_matches_two_sided_doubled(self):
        two = solve_boundaries(0.05, "two", [0.25, 0.5, 0.75, 1.0])
        one = solve_boundaries(0.025, "one", [0.25, 0.5, 0.75, 1.0])
        for left, right in zip(two["looks"], one["looks"]):
            self.assertAlmostEqual(left["boundary"], right["boundary"], places=6)

    def test_boundaries_calibrate_cumulative_not_independent_crossing(self):
        # Fixed-seed simulation of the whole multi-look process: per-look exit
        # rates must match spending increments, not independent p thresholds.
        fractions = [1 / 3, 2 / 3, 1.0]
        solution = solve_boundaries(0.05, "two", fractions)
        boundaries = [look["boundary"] for look in solution["looks"]]
        rng = random.Random("solver-cross-check")
        replications = 100000
        exits = [0] * 3
        for _ in range(replications):
            walk, previous = 0.0, 0.0
            for index, fraction in enumerate(fractions):
                walk += math.sqrt(fraction - previous) * rng.gauss(0.0, 1.0)
                if abs(walk / math.sqrt(fraction)) >= boundaries[index]:
                    exits[index] += 1
                    break
                previous = fraction
        total = sum(exits) / replications
        self.assertLess(abs(total - 0.05), 3 * math.sqrt(0.05 * 0.95 / replications))
        for index, look in enumerate(solution["looks"]):
            target = look["spend"]["increment_target"]
            self.assertLess(
                abs(exits[index] / replications - target),
                3 * math.sqrt(target * (1 - target) / replications) + 1e-4,
            )

    def test_solver_failure_states_are_visible(self):
        repeated = solve_boundaries(0.05, "two", [0.5, 0.5])
        self.assertEqual(repeated["status"], "failed")
        self.assertEqual(repeated["failure"]["reason"], "fraction_not_increasing")
        underflowed = solve_boundaries(0.05, "two", [1e-12, 1.0])
        self.assertEqual(underflowed["status"], "failed")
        self.assertEqual(
            underflowed["failure"]["reason"], "non_positive_spend_increment"
        )
        out_of_range = solve_boundaries(0.05, "two", [0.0, 1.0])
        self.assertEqual(out_of_range["status"], "failed")


class DesignRegistrationTests(InterimTestCase):
    def test_register_and_immutability(self):
        created = self.register_design()
        self.assertFalse(created["existing"])
        again = self.register_design()
        self.assertTrue(again["existing"])
        self.assertEqual(again["digest"], created["digest"])
        with self.assertRaises(VaultError) as caught:
            self.register_design(alpha="0.025")
        self.assertEqual(caught.exception.code, "conflict")
        # a changed plan must become a new trial design id
        replacement = self.register_design(design_id="study-v2", alpha="0.025")
        self.assertFalse(replacement["existing"])
        listed = self.vault.interim.designs()
        self.assertEqual([item["id"] for item in listed], ["study", "study-v2"])
        fetched = self.vault.interim.design("study")
        self.assertEqual(fetched["state"], "active")
        self.assertEqual(fetched["design"]["alpha"], "0.05")

    def test_invalid_designs_are_rejected(self):
        bad = [
            ({"alpha": "0"}, "design"),
            ({"alpha": "0.5"}, "design"),
            ({"alpha": "0.00001"}, "design"),
            ({"planned_fractions": ["0.5", "0.9"]}, "design"),
            ({"planned_fractions": ["0.75", "0.5", "1"]}, "design"),
            ({"planned_fractions": []}, "input"),
            ({"sidedness": "both"}, "design"),
            ({"benefit_direction": "higher"}, "design"),
            ({"min_batches_per_group": 5}, "input"),
            ({"specimens_per_batch": 1}, "input"),
            (
                {
                    "lock_rule": {
                        "batch_lock": "when_convenient",
                        "min_specimens_per_batch": 2,
                        "missing_value": "exclude",
                    }
                },
                "design",
            ),
            ({"assumptions": []}, "design"),
            ({"scale": {"common_sd": "0", "source": {"dataset": "pilot-run", "version": 1}}}, "design"),
            (
                {
                    "scale": {
                        "common_sd": "1.2",
                        "source": {"dataset": "trial", "version": 1},
                    }
                },
                "design",
            ),
            (
                {
                    "scale": {
                        "common_sd": "1.2",
                        "source": {"dataset": "pilot-run", "version": 9},
                    }
                },
                "missing",
            ),
        ]
        for overrides, code in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(VaultError) as caught:
                    self.register_design(**overrides)
                self.assertEqual(caught.exception.code, code)
        unequal = design_kwargs()
        unequal["schedule"] = unequal["schedule"][:-1]
        with self.assertRaises(VaultError) as caught:
            self.vault.interim.register_design(**unequal)
        self.assertEqual(caught.exception.code, "design")
        duplicated = design_kwargs()
        duplicated["schedule"][-1] = {"batch": "A1", "group": "B"}
        with self.assertRaises(VaultError):
            self.vault.interim.register_design(**duplicated)
        self.assertEqual(self.vault.interim.designs(), [])

    def test_design_checks_dataset_contract(self):
        self.vault.registry.add_schema(
            {
                "id": "loose",
                "version": 1,
                "fields": [
                    {"name": "batch", "type": "text"},
                    {"name": "specimen", "type": "text"},
                    {"name": "strength", "type": "text"},
                ],
                "primary_key": ["batch"],
            }
        )
        self.vault.registry.register("loose-data", "Loose", "loose", "operator")
        with self.assertRaises(VaultError) as caught:
            self.register_design(dataset="loose-data")
        self.assertEqual(caught.exception.code, "design")
        with self.assertRaises(VaultError):
            self.vault.interim.design("missing-design")


class LookFlowTests(InterimTestCase):
    def test_full_sequence_to_endpoint(self):
        self.register_design()
        self.ingest_trial(
            "v1",
            [
                ("A1", ["10.1", "10.3"]),
                ("A2", ["10.0", "10.2"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
            ],
        )
        look1 = self.vault.interim.look(
            design_id="study", look_id="look-1", dataset_version=1
        )
        self.assertFalse(look1["reused"])
        self.assertTrue(look1["formal"])
        self.assertEqual(look1["decision"], "continue")
        self.assertEqual(look1["counts"], {"A": 2, "B": 2})
        self.assertAlmostEqual(look1["information_fraction"], 0.5)
        self.assertAlmostEqual(look1["boundary"], 2.7718, places=3)
        self.assertAlmostEqual(look1["spend"]["cumulative_target"], 0.0055746, places=6)
        self.assertAlmostEqual(
            look1["spend"]["increment_target"],
            look1["spend"]["increment_achieved"],
            places=9,
        )
        self.assertEqual(look1["solver"]["status"], "ok")
        repeated = self.vault.interim.look(
            design_id="study", look_id="look-1", dataset_version=1
        )
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["sequence"], 1)
        with self.assertRaises(VaultError) as caught:
            self.vault.interim.look(design_id="study", look_id="look-1", dataset_version=2)
        self.assertEqual(caught.exception.code, "conflict")
        # same data version again: information did not increase
        stale = self.vault.interim.look(
            design_id="study", look_id="look-2", dataset_version=1
        )
        self.assertEqual(stale["decision"], "no_new_information")
        self.assertFalse(stale["formal"])
        self.assertIsNone(stale["spend"])
        self.ingest_trial(
            "v2",
            [
                ("A1", ["10.1", "10.3"]),
                ("A2", ["10.0", "10.2"]),
                ("A3", ["10.2", "10.4"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
                ("B3", ["10.0", "10.1"]),
            ],
            expected=1,
        )
        look3 = self.vault.interim.look(
            design_id="study", look_id="look-3", dataset_version=2
        )
        self.assertEqual(look3["decision"], "continue")
        self.assertAlmostEqual(look3["information_fraction"], 0.75)
        self.ingest_trial(
            "v3",
            [
                ("A1", ["10.1", "10.3"]),
                ("A2", ["10.0", "10.2"]),
                ("A3", ["10.2", "10.4"]),
                ("A4", ["10.1", None, "10.3"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
                ("B3", ["10.0", "10.1"]),
                ("B4", ["9.8", "10.0"]),
            ],
            expected=2,
        )
        look4 = self.vault.interim.look(
            design_id="study", look_id="look-4", dataset_version=3
        )
        self.assertEqual(look4["decision"], "endpoint")
        self.assertEqual(look4["conclusion"], "not_significant")
        self.assertAlmostEqual(look4["information_fraction"], 1.0)
        self.assertAlmostEqual(look4["spend"]["cumulative_achieved"], 0.05, places=6)
        self.assertEqual(look4["extraction"]["missing_specimens"]["total"], 1)
        self.assertEqual(
            look4["extraction"]["missing_specimens"]["by_batch"], {"A4": 1}
        )
        with self.assertRaises(VaultError) as caught:
            self.vault.interim.look(design_id="study", look_id="look-5", dataset_version=3)
        self.assertEqual(caught.exception.code, "state")
        report = self.vault.interim.report(design_id="study", dataset_version=3)
        self.assertEqual(report["state"], "closed")
        self.assertTrue(report["conclusions_valid"])
        self.assertEqual(len(report["looks"]), 4)
        self.assertEqual(report["post_closure_arrivals"]["new_evaluable_batches"], [])

    def test_stop_for_benefit_and_post_closure_arrivals(self):
        self.register_design()
        self.ingest_trial(
            "v1",
            [
                ("A1", ["13.0", "13.2"]),
                ("A2", ["12.8", "13.0"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
            ],
        )
        look = self.vault.interim.look(
            design_id="study", look_id="look-1", dataset_version=1
        )
        self.assertEqual(look["decision"], "stop_benefit")
        self.assertEqual(look["conclusion"], "significant")
        self.assertTrue(look["crossed"])
        self.ingest_trial(
            "v2",
            [
                ("A1", ["13.0", "13.2"]),
                ("A2", ["12.8", "13.0"]),
                ("A3", ["13.1", "13.3"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
                ("B3", ["10.1", "10.3"]),
            ],
            expected=1,
        )
        with self.assertRaises(VaultError) as caught:
            self.vault.interim.look(design_id="study", look_id="look-2", dataset_version=2)
        self.assertEqual(caught.exception.code, "state")
        report = self.vault.interim.report(design_id="study", dataset_version=2)
        self.assertEqual(report["state"], "closed")
        self.assertEqual(
            report["post_closure_arrivals"]["new_evaluable_batches"], ["A3", "B3"]
        )
        self.assertEqual(len(report["looks"]), 1)

    def test_insufficient_and_missing_data_are_separate(self):
        self.register_design()
        self.ingest_trial(
            "v1",
            [
                ("A1", ["10.1", "10.3"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", None]),
                ("X9", ["1.0", "1.1"]),
            ],
        )
        look = self.vault.interim.look(
            design_id="study", look_id="look-1", dataset_version=1
        )
        self.assertEqual(look["decision"], "insufficient_information")
        self.assertFalse(look["formal"])
        extraction = look["extraction"]
        self.assertEqual(extraction["missing_batches"], ["A2", "A3", "A4", "B3", "B4"])
        self.assertEqual(
            extraction["incomplete_batches"],
            [{"batch": "B2", "group": "B", "valid_specimens": 1, "required_specimens": 2}],
        )
        self.assertEqual(extraction["unscheduled_batches"], ["X9"])
        self.assertEqual(extraction["missing_specimens"]["by_batch"], {"B2": 1})
        self.ingest_trial(
            "v2",
            [
                ("A1", ["10.1", "10.3"]),
                ("A2", ["10.0", "10.2"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
            ],
            expected=1,
        )
        formal = self.vault.interim.look(
            design_id="study", look_id="look-2", dataset_version=2
        )
        self.assertTrue(formal["formal"])
        self.assertEqual(formal["sequence"], 2)

    def test_look_requires_registered_schema_version(self):
        self.register_design()
        evolved = specimen_schema(version=2)
        evolved["fields"].append(
            {"name": "note", "type": "text", "required": False}
        )
        self.vault.registry.add_schema(evolved)
        self.vault.ingestion.ingest(
            "v1",
            "trial",
            2,
            jsonl(specimen_rows([("A1", ["10.1", "10.2"])])),
            "jsonl",
            "internal",
            ["research"],
        )
        with self.assertRaises(VaultError) as caught:
            self.vault.interim.look(design_id="study", look_id="look-1", dataset_version=1)
        self.assertEqual(caught.exception.code, "schema")


class WithdrawalTests(InterimTestCase):
    def test_withdrawal_keeps_history_but_blocks_new_conclusions(self):
        self.register_design()
        self.ingest_trial(
            "v1",
            [
                ("A1", ["10.1", "10.3"]),
                ("A2", ["10.0", "10.2"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
            ],
        )
        look = self.vault.interim.look(
            design_id="study", look_id="look-1", dataset_version=1
        )
        self.assertEqual(look["decision"], "continue")
        self.vault.registry.withdraw("trial", 1, "Source data corrupted")
        with self.assertRaises(VaultError) as caught:
            self.vault.interim.look(design_id="study", look_id="look-2", dataset_version=1)
        self.assertEqual(caught.exception.code, "lineage")
        report = self.vault.interim.report(design_id="study", dataset_version=1)
        self.assertFalse(report["conclusions_valid"])
        self.assertIn("data_versions_withdrawn", report["warnings"])
        self.assertEqual(report["versions_state"], {"1": "withdrawn"})
        self.assertEqual(len(report["looks"]), 1)
        self.assertEqual(report["looks"][0]["decision"], "continue")
        self.assertEqual(report["state"], "active")

    def test_pilot_withdrawal_is_flagged(self):
        self.register_design()
        self.vault.registry.withdraw("pilot-run", 1, "Pilot review reopened")
        report = self.vault.interim.report(design_id="study")
        self.assertIn("pilot_scale_source_withdrawn", report["warnings"])
        self.assertTrue(report["conclusions_valid"])


class ValidationTests(InterimTestCase):
    def test_validation_is_deterministic_and_calibrated(self):
        self.register_design()
        first = self.vault.interim.validate(
            design_id="study", seed="fixed-seed", effect="2.5", replications=20000
        )
        second = self.vault.interim.validate(
            design_id="study", seed="fixed-seed", effect="2.5", replications=20000
        )
        self.assertEqual(first, second)
        null = first["null"]
        self.assertEqual(null["target_alpha"], 0.05)
        self.assertLessEqual(null["interval95"][0], 0.05)
        self.assertGreaterEqual(null["interval95"][1], 0.05)
        self.assertLess(abs(null["rate"] - 0.05), 0.01)
        self.assertEqual(first["calibration"]["simulation_draws"], 0)
        alternative = first["alternative"]
        self.assertGreater(alternative["rate"], 0.9)
        self.assertGreater(alternative["early_stops"], 0)
        self.assertLess(alternative["mean_stop_fraction"], 1.0)
        self.assertEqual(
            sum(alternative["stops_by_look"]), alternative["crossings"]
        )
        other = self.vault.interim.validate(
            design_id="study", seed="different-seed", effect="2.5", replications=20000
        )
        self.assertNotEqual(first["null"]["crossings"], other["null"]["crossings"])
        self.assertEqual(self.vault.interim.design("study")["looks"], 0)

    def test_validation_inputs_are_bounded(self):
        self.register_design()
        with self.assertRaises(VaultError):
            self.vault.interim.validate(design_id="study", seed="", effect="1.0")
        with self.assertRaises(VaultError):
            self.vault.interim.validate(design_id="study", seed="s", effect="0")
        with self.assertRaises(VaultError):
            self.vault.interim.validate(design_id="study", seed="s", effect="-1")
        with self.assertRaises(VaultError):
            self.vault.interim.validate(
                design_id="study", seed="s", effect="1.0", replications=10
            )


class IntegrityTests(InterimTestCase):
    def test_inspect_covers_interim_records(self):
        self.register_design()
        self.ingest_trial(
            "v1",
            [
                ("A1", ["10.1", "10.3"]),
                ("A2", ["10.0", "10.2"]),
                ("B1", ["9.9", "10.1"]),
                ("B2", ["10.0", "10.2"]),
            ],
        )
        self.vault.interim.look(design_id="study", look_id="look-1", dataset_version=1)
        status = self.vault.checks.inspect()
        self.assertEqual(status["interim"], {"designs": 1, "looks": 1})
        self.vault.database.connection.execute(
            "UPDATE interim_looks SET payload=? WHERE design=? AND id=?",
            ('{"tampered": true}', "study", "look-1"),
        )
        with self.assertRaises(VaultError) as caught:
            self.vault.checks.inspect()
        self.assertEqual(caught.exception.code, "integrity")


if __name__ == "__main__":
    unittest.main()
