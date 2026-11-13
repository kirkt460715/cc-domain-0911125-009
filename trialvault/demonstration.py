import tempfile
from pathlib import Path
from .service import Vault
from .validation import canonical
from .releases.exporter import verify_directory


def _jsonl(rows):
    return ("\n".join(canonical(row) for row in rows) + "\n").encode()


def _demonstrate_interim(vault):
    pilot_schema = {
        "id": "pilot-readings",
        "version": 1,
        "fields": [
            {"name": "id", "type": "text"},
            {"name": "reading", "type": "decimal"},
        ],
        "primary_key": ["id"],
    }
    vault.registry.add_schema(pilot_schema)
    vault.registry.register("pilot-run", "Independent pilot readings", "pilot-readings", "operator")
    vault.ingestion.ingest(
        "pilot",
        "pilot-run",
        1,
        _jsonl([{"id": "p1", "reading": "10.2"}, {"id": "p2", "reading": "9.8"}]),
        "jsonl",
        "internal",
        ["research"],
    )
    specimen_schema = {
        "id": "specimens",
        "version": 1,
        "fields": [
            {"name": "batch", "type": "text"},
            {"name": "specimen", "type": "text"},
            {"name": "strength", "type": "decimal", "nullable": True},
        ],
        "primary_key": ["batch", "specimen"],
    }
    vault.registry.add_schema(specimen_schema)
    vault.registry.register(
        "strength-trial", "Batch-randomized strength trial", "specimens", "operator"
    )
    schedule = [
        {"batch": f"A{index}", "group": "A"} for index in range(1, 5)
    ] + [{"batch": f"B{index}", "group": "B"} for index in range(1, 5)]
    design = vault.interim.register_design(
        design_id="strength-study",
        dataset="strength-trial",
        schema_version=1,
        fields={"batch": "batch", "specimen": "specimen", "value": "strength"},
        schedule=schedule,
        benefit_direction="A_minus_B",
        sidedness="two",
        alpha="0.05",
        max_batches_per_group=4,
        planned_fractions=["0.5", "1"],
        min_batches_per_group=2,
        specimens_per_batch=2,
        lock_rule={
            "batch_lock": "dataset_version",
            "min_specimens_per_batch": 2,
            "missing_value": "exclude",
        },
        scale={
            "common_sd": "1.2",
            "source": {"dataset": "pilot-run", "version": 1},
            "note": "Common specimen-level SD frozen from the independent pilot",
        },
        assumptions=[
            "Batch summaries are approximately normal",
            "Batches are independent",
            "Common specimen-level SD frozen from pilot, not re-estimated",
            "Equal allocation of batches to groups",
        ],
    )

    def specimen_rows(batches):
        rows = []
        for batch, values in batches:
            for index, value in enumerate(values, 1):
                rows.append(
                    {
                        "batch": batch,
                        "specimen": f"{batch}-S{index}",
                        "strength": value,
                    }
                )
        return rows

    first = specimen_rows(
        [
            ("A1", ["10.1", "10.3"]),
            ("A2", ["10.0", "10.2"]),
            ("B1", ["9.9", "10.1"]),
            ("B2", ["10.0", "10.2"]),
        ]
    )
    vault.ingestion.ingest(
        "strength-v1", "strength-trial", 1, _jsonl(first), "jsonl", "internal", ["research"]
    )
    look1 = vault.interim.look(
        design_id="strength-study", look_id="look-1", dataset_version=1
    )
    second = first + specimen_rows(
        [
            ("A3", ["10.2", "10.4"]),
            ("A4", ["10.1", None, "10.3"]),
            ("B3", ["10.0", "10.1"]),
            ("B4", ["9.8", "10.0"]),
        ]
    )
    vault.ingestion.ingest(
        "strength-v2",
        "strength-trial",
        1,
        _jsonl(second),
        "jsonl",
        "internal",
        ["research"],
        expected_version=1,
    )
    look2 = vault.interim.look(
        design_id="strength-study", look_id="look-2", dataset_version=2
    )
    validation = vault.interim.validate(
        design_id="strength-study", seed="demo-validation", effect="1.5", replications=2000
    )
    report = vault.interim.report(design_id="strength-study", dataset_version=2)
    assert look1["decision"] == "continue"
    assert look2["decision"] == "endpoint"
    assert report["state"] == "closed"
    assert validation["calibration"]["simulation_draws"] == 0
    return {
        "design": {"id": design["id"], "digest": design["digest"]},
        "looks": [
            {
                "look": look1["look"],
                "decision": look1["decision"],
                "statistic": look1["statistic"],
                "boundary": look1["boundary"],
                "spend": look1["spend"],
            },
            {
                "look": look2["look"],
                "decision": look2["decision"],
                "statistic": look2["statistic"],
                "boundary": look2["boundary"],
                "spend": look2["spend"],
                "missing_specimens": look2["extraction"]["missing_specimens"],
            },
        ],
        "validation": {
            "null": validation["null"],
            "alternative": validation["alternative"],
            "calibration": validation["calibration"],
        },
        "report_state": report["state"],
        "conclusions_valid": report["conclusions_valid"],
    }


def demonstrate():
    with tempfile.TemporaryDirectory() as directory:
        with Vault(Path(directory) / "vault") as vault:
            schema = {
                "id": "measurements",
                "version": 1,
                "fields": [
                    {"name": "id", "type": "text"},
                    {"name": "pressure", "type": "decimal", "minimum": "0"},
                ],
                "primary_key": ["id"],
            }
            vault.registry.add_schema(schema)
            vault.registry.register(
                "trial", "Synthetic trial measurements", "measurements", "operator"
            )
            content = (
                canonical({"id": "sample-A", "pressure": "12.5"})
                + "\n"
                + canonical({"id": "sample-B", "pressure": "13"})
                + "\n"
            ).encode()
            ingested = vault.ingestion.ingest(
                "request", "trial", 1, content, "jsonl", "internal", ["research"]
            )
            vault.releases.plan(
                "release", [{"dataset": "trial", "version": 1}], "research", "internal"
            )
            vault.releases.approve(
                "release", 1, "independent-reviewer", "Synthetic dataset verified"
            )
            exported = vault.exporter.export(
                "export", "release", Path(directory) / "release"
            )
            verified = verify_directory(Path(directory) / "release")
            interim = _demonstrate_interim(vault)
            assert ingested["rows"] == 2
            assert verified["rows"] == 2
            assert exported["state"] == "complete"
            return {
                "ingestion": ingested,
                "export": exported,
                "verification": verified,
                "interim": interim,
                "integrity": vault.checks.inspect(),
            }
