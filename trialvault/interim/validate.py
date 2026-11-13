import math
import random
from ..validation import integer, decimal, decimal_text, digest, reject
from .spending import solve_boundaries
from .statistics import wilson_interval

STREAM_LABEL = "trialvault-interim-validation"


class InterimValidation:
    """Independent fixed-seed simulation of the whole multi-look process.

    Boundaries come from deterministic quadrature (zero simulated draws), so
    no tuning sample exists; validation streams are derived per design digest,
    seed, and world, and cannot overlap with any calibration sample.
    """

    def __init__(self, database, designs):
        self.db = database
        self.designs = designs

    def validate(self, design_id, seed, effect, replications=10000):
        design = self.designs.get(design_id)
        if not isinstance(seed, str) or not seed.strip() or len(seed) > 256:
            reject("input", "validation seed must be bounded text")
        seed = seed.strip()
        replications = integer(replications, 1000, 200000)
        effect_value = decimal(effect)
        if effect_value <= 0:
            reject("input", "validation effect must be positive in the benefit direction")
        alpha = float(design["alpha"])
        fractions = [float(value) for value in design["planned_fractions"]]
        solution = solve_boundaries(alpha, design["sidedness"], fractions)
        design_digest = digest(design)
        report = {
            "design": design_id,
            "design_digest": design_digest,
            "seed": seed,
            "replications": replications,
            "grid": {
                "fractions": fractions,
                "solver_status": solution["status"],
                "boundaries": [
                    look["boundary"] for look in solution["looks"]
                ],
                "cumulative_target": solution.get("cumulative_target"),
                "cumulative_achieved": solution.get("cumulative_achieved"),
                "solver": [look["solver"] for look in solution["looks"]],
            },
            "calibration": {
                "method": "deterministic Gauss-Legendre quadrature",
                "simulation_draws": 0,
            },
            "independence": (
                "boundaries are solved without any simulated samples; validation "
                "streams are seeded per design digest and world and are not reused "
                "from any calibration"
            ),
        }
        if solution["status"] != "ok":
            report["failure"] = solution["failure"]
            self._audit(design_id, seed, replications, effect_value, report)
            return report
        sigma = float(design["scale"]["common_sd"])
        per_batch = design["specimens_per_batch"]
        maximum = design["max_batches_per_group"]
        drift = float(effect_value) * math.sqrt(per_batch * maximum / 2.0) / sigma
        two_sided = design["sidedness"] == "two"
        boundaries = [look["boundary"] for look in solution["looks"]]
        for world, theta in (("null", 0.0), ("alternative", drift)):
            rng = random.Random(f"{STREAM_LABEL}:{design_digest}:{seed}:{world}")
            stops = [0] * len(fractions)
            stop_fraction_total = 0.0
            for _ in range(replications):
                walk = 0.0
                previous = 0.0
                for index, fraction in enumerate(fractions):
                    walk += math.sqrt(fraction - previous) * rng.gauss(0.0, 1.0)
                    walk += theta * (fraction - previous)
                    statistic = walk / math.sqrt(fraction)
                    boundary = boundaries[index]
                    crossed = (
                        abs(statistic) >= boundary
                        if two_sided
                        else statistic >= boundary
                    )
                    if crossed:
                        stops[index] += 1
                        stop_fraction_total += fraction
                        break
                    previous = fraction
            crossings = sum(stops)
            report[world] = {
                "effect": "0" if world == "null" else decimal_text(effect_value),
                "drift": theta,
                "crossings": crossings,
                "rate": crossings / replications,
                "interval95": wilson_interval(crossings, replications),
                "stops_by_look": stops,
                "early_stops": sum(stops[:-1]),
                "mean_stop_fraction": (
                    stop_fraction_total / crossings if crossings else None
                ),
            }
        report["null"]["target_alpha"] = alpha
        self._audit(design_id, seed, replications, effect_value, report)
        return report

    def _audit(self, design_id, seed, replications, effect_value, report):
        with self.db.transaction():
            self.db.audit(
                "interim_validated",
                design_id,
                {
                    "seed": seed,
                    "replications": replications,
                    "effect": decimal_text(effect_value),
                    "null_rate": report.get("null", {}).get("rate"),
                    "solver_status": report["grid"]["solver_status"],
                    "digest": digest(report),
                },
            )
