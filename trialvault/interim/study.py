import math
from ..validation import (
    name,
    integer,
    canonical,
    digest,
    parse_json,
    reject,
)
from .spending import solve_boundaries

MAX_LOOKS = 50
MAX_FORMAL_LOOKS = 25
ENDPOINT_FRACTION = 1.0 - 1e-12
BOUNDARY_DRIFT_TOLERANCE = 1e-9
CLOSING_DECISIONS = ("stop_benefit", "endpoint")


def extract_batches(design, rows):
    """Summarize locked specimen rows into batch-level analysis units.

    Whole missing batches and missing specimens are reported separately;
    group membership comes only from the pre-registered schedule.
    """
    fields = design["fields"]
    schedule = {entry["batch"]: entry["group"] for entry in design["schedule"]}
    min_specimens = design["lock_rule"]["min_specimens_per_batch"]
    found = {}
    unscheduled = set()
    for row in rows:
        batch = row[fields["batch"]]
        if batch not in schedule:
            unscheduled.add(batch)
            continue
        entry = found.setdefault(batch, {"valid": 0, "total": 0.0, "missing": 0})
        value = row[fields["value"]]
        if value is None:
            entry["missing"] += 1
        else:
            entry["valid"] += 1
            entry["total"] += float(value)
    evaluable = []
    missing_batches = []
    incomplete = []
    missing_by_batch = {}
    missing_total = 0
    for batch in sorted(schedule):
        group = schedule[batch]
        entry = found.get(batch)
        if entry is None:
            missing_batches.append(batch)
            continue
        if entry["missing"]:
            missing_by_batch[batch] = entry["missing"]
            missing_total += entry["missing"]
        if entry["valid"] >= min_specimens:
            evaluable.append(
                {
                    "batch": batch,
                    "group": group,
                    "specimens": entry["valid"],
                    "mean": entry["total"] / entry["valid"],
                }
            )
        else:
            incomplete.append(
                {
                    "batch": batch,
                    "group": group,
                    "valid_specimens": entry["valid"],
                    "required_specimens": min_specimens,
                }
            )
    return {
        "evaluable": evaluable,
        "missing_batches": missing_batches,
        "incomplete_batches": incomplete,
        "unscheduled_batches": sorted(unscheduled),
        "missing_specimens": {"total": missing_total, "by_batch": missing_by_batch},
    }


def evaluate_look(design, extraction, formal_history):
    """Evaluate one look against the registered design and formal history."""
    counts = {"A": 0, "B": 0}
    totals = {"A": 0.0, "B": 0.0}
    for batch in extraction["evaluable"]:
        counts[batch["group"]] += 1
        totals[batch["group"]] += batch["mean"]
    base = {
        "formal": False,
        "counts": counts,
        "information_fraction": None,
        "statistic": None,
        "boundary": None,
        "crossed": None,
        "spend": None,
        "solver": None,
        "conclusion": None,
        "group_means": None,
    }
    minimum = design["min_batches_per_group"]
    if counts["A"] < minimum or counts["B"] < minimum:
        return {**base, "decision": "insufficient_information"}
    maximum = design["max_batches_per_group"]
    fraction = (
        2.0 * counts["A"] * counts["B"] / (maximum * (counts["A"] + counts["B"]))
    )
    base["information_fraction"] = fraction
    if formal_history and fraction <= formal_history[-1]["information_fraction"]:
        return {**base, "decision": "no_new_information"}
    if len(formal_history) >= MAX_FORMAL_LOOKS:
        reject("state", "formal look limit reached for design")
    alpha = float(design["alpha"])
    fractions = [look["information_fraction"] for look in formal_history] + [fraction]
    solution = solve_boundaries(alpha, design["sidedness"], fractions)
    if solution["status"] != "ok":
        return {
            **base,
            "formal": True,
            "decision": "solver_failed",
            "solver": solution["failure"],
        }
    for stored, solved in zip(formal_history, solution["looks"]):
        if abs(stored["boundary"] - solved["boundary"]) > BOUNDARY_DRIFT_TOLERANCE:
            reject("integrity", "boundary path drifted from stored look history")
    current = solution["looks"][-1]
    boundary = current["boundary"]
    sigma = float(design["scale"]["common_sd"])
    per_batch = design["specimens_per_batch"]
    means = {group: totals[group] / counts[group] for group in ("A", "B")}
    difference = means["A"] - means["B"]
    if design["benefit_direction"] == "B_minus_A":
        difference = -difference
    standard_error = sigma * math.sqrt(
        (1.0 / counts["A"] + 1.0 / counts["B"]) / per_batch
    )
    statistic = difference / standard_error
    if design["sidedness"] == "two":
        crossed = abs(statistic) >= boundary
    else:
        crossed = statistic >= boundary
    if crossed:
        decision, conclusion = "stop_benefit", "significant"
    elif fraction >= ENDPOINT_FRACTION:
        decision, conclusion = "endpoint", "not_significant"
    else:
        decision, conclusion = "continue", None
    return {
        **base,
        "formal": True,
        "decision": decision,
        "conclusion": conclusion,
        "statistic": statistic,
        "boundary": boundary,
        "crossed": crossed,
        "group_means": means,
        "spend": {
            "increment_target": current["spend"]["increment_target"],
            "increment_achieved": current["spend"]["increment_achieved"],
            "cumulative_target": solution["cumulative_target"],
            "cumulative_achieved": solution["cumulative_achieved"],
        },
        "solver": current["solver"],
    }


class InterimStudy:
    def __init__(self, database, registry, ingestion, designs):
        self.db = database
        self.registry = registry
        self.ingestion = ingestion
        self.designs = designs

    def _looks(self, design_id):
        return [
            parse_json(row["payload"])
            for row in self.db.rows(
                "SELECT payload FROM interim_looks WHERE design=? ORDER BY sequence",
                (design_id,),
            )
        ]

    def state(self, design_id):
        history = self._looks(design_id)
        formal = [look for look in history if look.get("formal")]
        invalid = any(
            look["decision"] == "solver_failed" for look in formal
        )
        closed = bool(formal) and formal[-1]["decision"] in CLOSING_DECISIONS
        return {
            "state": "invalid" if invalid else ("closed" if closed else "active"),
            "looks": len(history),
            "formal_looks": len(formal),
            "last_decision": history[-1]["decision"] if history else None,
        }

    def look(self, design_id, look_id, dataset_version):
        design = self.designs.get(design_id)
        look_id = name(look_id)
        dataset_version = integer(dataset_version, 1)
        request = {
            "design": design_id,
            "look": look_id,
            "dataset_version": dataset_version,
        }
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT request,payload FROM interim_looks WHERE design=? AND id=?",
                (design_id, look_id),
            )
            if old:
                if old["request"] != canonical(request):
                    reject("conflict", "look id has a different recorded request")
                return {**parse_json(old["payload"]), "reused": True}
            history = self._looks(design_id)
            if len(history) >= MAX_LOOKS:
                reject("state", "look limit reached for design")
            formal = [look for look in history if look.get("formal")]
            if formal and formal[-1]["decision"] in CLOSING_DECISIONS:
                reject(
                    "state",
                    "trial is closed; later batches are reported separately and the trial cannot be reopened",
                )
            if any(look["decision"] == "solver_failed" for look in formal):
                reject(
                    "state",
                    "analysis halted: an earlier boundary solve failed",
                )
            record = self.registry.version(design["dataset"], dataset_version)
            if record["state"] != "active":
                reject(
                    "lineage",
                    "data version is withdrawn; historical decisions are retained but no new conclusions are published",
                )
            if record["schema_version"] != design["schema_version"]:
                reject("schema", "data version schema differs from registered design")
            extraction = extract_batches(
                design, self.ingestion.rows(design["dataset"], dataset_version)
            )
            result = evaluate_look(design, extraction, formal)
            result.update(
                {
                    "design": design_id,
                    "look": look_id,
                    "sequence": len(history) + 1,
                    "dataset": design["dataset"],
                    "version": dataset_version,
                    "data_digest": record["digest"],
                    "extraction": extraction,
                }
            )
            con.execute(
                "INSERT INTO interim_looks VALUES(?,?,?,?,?,?)",
                (
                    design_id,
                    look_id,
                    result["sequence"],
                    canonical(request),
                    canonical(result),
                    digest(result),
                ),
            )
            self.db.audit(
                "interim_look_recorded",
                design_id,
                {
                    "look": look_id,
                    "sequence": result["sequence"],
                    "decision": result["decision"],
                    "formal": result["formal"],
                    "digest": digest(result),
                },
            )
        return {**result, "reused": False}

    def report(self, design_id, dataset_version=None):
        design = self.designs.get(design_id)
        history = self._looks(design_id)
        formal = [look for look in history if look.get("formal")]
        closing = (
            formal[-1]
            if formal and formal[-1]["decision"] in CLOSING_DECISIONS
            else None
        )
        state = self.state(design_id)["state"]
        versions_used = sorted({look["version"] for look in history})
        versions_state = {}
        withdrawn = []
        for version in versions_used:
            current = self.registry.version(design["dataset"], version)["state"]
            versions_state[str(version)] = current
            if current != "active":
                withdrawn.append(version)
        source = design["scale"]["source"]
        pilot_state = self.registry.version(source["dataset"], source["version"])[
            "state"
        ]
        warnings = []
        if pilot_state != "active":
            warnings.append("pilot_scale_source_withdrawn")
        if withdrawn:
            warnings.append("data_versions_withdrawn")
        arrivals = None
        if closing is not None:
            version = dataset_version
            if version is None:
                row = self.db.one(
                    "SELECT MAX(version) AS latest FROM versions WHERE dataset=?",
                    (design["dataset"],),
                )
                version = row["latest"]
            if version is not None:
                version = integer(version, 1)
                record = self.registry.version(design["dataset"], version)
                if record["state"] == "active":
                    extraction = extract_batches(
                        design, self.ingestion.rows(design["dataset"], version)
                    )
                    analyzed = {
                        batch["batch"]
                        for batch in closing["extraction"]["evaluable"]
                    }
                    now = {batch["batch"] for batch in extraction["evaluable"]}
                    arrivals = {
                        "version": version,
                        "new_evaluable_batches": sorted(now - analyzed),
                        "note": "batches arriving after closure are listed separately and are not analyzed",
                    }
                else:
                    arrivals = {
                        "version": version,
                        "state": record["state"],
                        "new_evaluable_batches": None,
                        "note": "data version withdrawn; arrivals are not published",
                    }
        return {
            "design": design,
            "design_digest": digest(design),
            "state": state,
            "conclusions_valid": state != "invalid" and not withdrawn,
            "warnings": warnings,
            "versions_state": versions_state,
            "looks": history,
            "post_closure_arrivals": arrivals,
        }
