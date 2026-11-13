from decimal import Decimal
from ..validation import (
    name,
    integer,
    object_value,
    list_value,
    decimal,
    decimal_text,
    canonical,
    digest,
    parse_json,
    reject,
)

GROUPS = ("A", "B")
BENEFIT_DIRECTIONS = ("A_minus_B", "B_minus_A")
SIDEDNESS = ("one", "two")
BATCH_LOCK_RULES = ("dataset_version",)
MISSING_VALUE_RULES = ("exclude",)
MIN_ALPHA = Decimal("0.0001")
MAX_ALPHA = Decimal("0.5")


def normalize_design(
    design_id,
    dataset,
    schema_version,
    fields,
    schedule,
    benefit_direction,
    sidedness,
    alpha,
    max_batches_per_group,
    planned_fractions,
    min_batches_per_group,
    specimens_per_batch,
    lock_rule,
    scale,
    assumptions,
):
    """Validate and normalize a pre-registered group sequential design.

    The returned payload is canonical: once stored it is immutable, and any
    change after results are seen requires registering a new design id.
    """
    design_id = name(design_id)
    dataset = name(dataset)
    schema_version = integer(schema_version, 1, 100000)
    object_value(fields, {"batch", "specimen", "value"}, {"batch", "specimen", "value"})
    field_map = {
        "batch": name(fields["batch"]),
        "specimen": name(fields["specimen"]),
        "value": name(fields["value"]),
    }
    if benefit_direction not in BENEFIT_DIRECTIONS:
        reject("design", "benefit direction must name the group difference")
    if sidedness not in SIDEDNESS:
        reject("design", "sidedness must be one or two")
    alpha_value = decimal(alpha)
    if not MIN_ALPHA <= alpha_value < MAX_ALPHA:
        reject("design", "alpha must be within [0.0001, 0.5)")
    maximum = integer(max_batches_per_group, 1, 500)
    fractions = []
    for item in list_value(planned_fractions, 1, 10):
        value = decimal(item)
        if not Decimal(0) < value <= Decimal(1):
            reject("design", "planned information fractions must lie in (0, 1]")
        fractions.append(value)
    if any(earlier >= later for earlier, later in zip(fractions, fractions[1:])):
        reject("design", "planned information fractions must strictly increase")
    if fractions[-1] != Decimal(1):
        reject("design", "final planned information fraction must be 1")
    minimum = integer(min_batches_per_group, 1, maximum)
    per_batch = integer(specimens_per_batch, 1, 100000)
    object_value(
        lock_rule,
        {"batch_lock", "min_specimens_per_batch", "missing_value"},
        {"batch_lock", "min_specimens_per_batch", "missing_value"},
    )
    if lock_rule["batch_lock"] not in BATCH_LOCK_RULES:
        reject("design", "unsupported batch lock rule")
    if lock_rule["missing_value"] not in MISSING_VALUE_RULES:
        reject("design", "unsupported missing value rule")
    min_specimens = integer(lock_rule["min_specimens_per_batch"], 1, per_batch)
    object_value(scale, {"common_sd", "source", "note"}, {"common_sd", "source"})
    common_sd = decimal(scale["common_sd"])
    if common_sd <= 0:
        reject("design", "frozen common standard deviation must be positive")
    object_value(scale["source"], {"dataset", "version"}, {"dataset", "version"})
    pilot_dataset = name(scale["source"]["dataset"])
    pilot_version = integer(scale["source"]["version"], 1)
    note = scale.get("note", "")
    if not isinstance(note, str) or len(note) > 1024:
        reject("design", "scale note must be bounded text")
    entries = []
    seen = set()
    counts = {group: 0 for group in GROUPS}
    for entry in list_value(schedule, 2, 1000):
        object_value(entry, {"batch", "group"}, {"batch", "group"})
        batch = name(entry["batch"])
        if entry["group"] not in GROUPS:
            reject("design", "schedule group must be A or B")
        if batch in seen:
            reject("design", "duplicate scheduled batch", batch=batch)
        seen.add(batch)
        counts[entry["group"]] += 1
        entries.append({"batch": batch, "group": entry["group"]})
    if counts["A"] != maximum or counts["B"] != maximum:
        reject(
            "design",
            "schedule must assign exactly max_batches_per_group batches to each group",
            counts=counts,
        )
    entries.sort(key=lambda item: item["batch"])
    if (
        not isinstance(assumptions, list)
        or not 1 <= len(assumptions) <= 20
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 512
            for item in assumptions
        )
    ):
        reject("design", "assumptions must be a bounded list of bounded text")
    return {
        "format": 1,
        "kind": "interim_sequential_design",
        "id": design_id,
        "dataset": dataset,
        "schema_version": schema_version,
        "fields": field_map,
        "schedule": entries,
        "benefit_direction": benefit_direction,
        "sidedness": sidedness,
        "alpha": decimal_text(alpha_value),
        "max_batches_per_group": maximum,
        "planned_fractions": [decimal_text(value) for value in fractions],
        "min_batches_per_group": minimum,
        "specimens_per_batch": per_batch,
        "lock_rule": {
            "batch_lock": lock_rule["batch_lock"],
            "min_specimens_per_batch": min_specimens,
            "missing_value": lock_rule["missing_value"],
        },
        "scale": {
            "common_sd": decimal_text(common_sd),
            "source": {"dataset": pilot_dataset, "version": pilot_version},
            "note": note,
        },
        "assumptions": [item.strip() for item in assumptions],
    }


class InterimDesigns:
    def __init__(self, database, registry):
        self.db = database
        self.registry = registry

    def register(self, **kwargs):
        payload = normalize_design(**kwargs)
        dataset = self.registry.dataset(payload["dataset"])
        contract = self.registry.schema(dataset["schema_id"], payload["schema_version"])
        for role in ("batch", "specimen"):
            field = contract.by_name.get(payload["fields"][role])
            if field is None or field.type not in {"text", "enum"}:
                reject("design", "batch and specimen fields must be text", role=role)
        value_field = contract.by_name.get(payload["fields"]["value"])
        if value_field is None or value_field.type not in {"decimal", "integer"}:
            reject("design", "endpoint value field must be numeric")
        if contract.primary_key != [
            payload["fields"]["batch"],
            payload["fields"]["specimen"],
        ]:
            reject(
                "design",
                "specimen dataset primary key must be (batch, specimen)",
            )
        source = payload["scale"]["source"]
        if source["dataset"] == payload["dataset"]:
            reject("design", "pilot scale source must be an independent dataset")
        pilot = self.registry.version(source["dataset"], source["version"])
        if pilot["state"] != "active":
            reject("design", "pilot scale source version is not active")
        checksum = digest(payload)
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT digest FROM interim_designs WHERE id=?", (payload["id"],)
            )
            if old:
                if old["digest"] != checksum:
                    reject(
                        "conflict",
                        "interim design is immutable; register a new design id for a changed plan",
                    )
                return {"id": payload["id"], "digest": checksum, "existing": True}
            con.execute(
                "INSERT INTO interim_designs VALUES(?,?,?)",
                (payload["id"], canonical(payload), checksum),
            )
            self.db.audit(
                "interim_design_registered", payload["id"], {"digest": checksum}
            )
        return {
            "id": payload["id"],
            "digest": checksum,
            "existing": False,
            "design": payload,
        }

    def get(self, design_id):
        name(design_id)
        row = self.db.one(
            "SELECT payload,digest FROM interim_designs WHERE id=?", (design_id,)
        )
        if row is None:
            reject("missing", "interim design not found")
        payload = parse_json(row["payload"])
        if digest(payload) != row["digest"]:
            reject("integrity", "interim design digest mismatch")
        return payload

    def list(self):
        return [
            {"id": row["id"], "digest": row["digest"]}
            for row in self.db.rows("SELECT id,digest FROM interim_designs ORDER BY id")
        ]
