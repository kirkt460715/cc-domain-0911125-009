import math
from .statistics import norm_cdf, norm_pdf, norm_ppf, norm_sf, gauss_legendre

QUADRATURE_POINTS = 40
SOLVER_TOLERANCE = 1e-12
ACCEPT_TOLERANCE = 1e-9
ONE_SIDED_TAIL = 10.0
MAX_ITERATIONS = 100
MAX_BOUNDARY = 16.0


def cumulative_spending(alpha, sidedness, fraction):
    """O'Brien-Fleming type alpha spending: cumulative spend at an information fraction."""
    if sidedness == "two":
        return 2.0 * norm_sf(norm_ppf(1.0 - alpha / 2.0) / math.sqrt(fraction))
    return norm_sf(norm_ppf(1.0 - alpha) / math.sqrt(fraction))


def solve_boundaries(alpha, sidedness, fractions, quadrature_points=QUADRATURE_POINTS):
    """Calibrate sequential boundaries for an information fraction sequence.

    Boundaries are solved so that the cumulative crossing probability of the
    correlated sequential statistics (Brownian-motion covariance) equals the
    O'Brien-Fleming spending target at each look; per-look spends are never
    treated as independent p-value thresholds. Every look reports the solver
    error and failure states are returned explicitly instead of raising.
    """
    looks = []
    grid = None
    previous_mass = 1.0
    previous_fraction = 0.0
    cumulative_target = 0.0
    for index, fraction in enumerate(fractions):
        if not 0.0 < fraction <= 1.0 or (index > 0 and fraction <= previous_fraction):
            return _failed(looks, index, "fraction_not_increasing", fraction)
        target = cumulative_spending(alpha, sidedness, fraction)
        increment = target - cumulative_target
        if not increment > 0.0:
            return _failed(looks, index, "non_positive_spend_increment", fraction)
        if grid is None:
            if sidedness == "two":

                def continuation(c):
                    return norm_cdf(c) - norm_cdf(-c)

            else:

                def continuation(c):
                    return norm_cdf(c)

        else:
            nodes, weights, density = grid
            shift = math.sqrt(previous_fraction / fraction)
            spread = math.sqrt((fraction - previous_fraction) / fraction)
            if sidedness == "two":

                def continuation(c, nodes=nodes, weights=weights, density=density, shift=shift, spread=spread):
                    return sum(
                        w
                        * h
                        * (
                            norm_cdf((c - shift * u) / spread)
                            - norm_cdf((-c - shift * u) / spread)
                        )
                        for u, w, h in zip(nodes, weights, density)
                    )

            else:

                def continuation(c, nodes=nodes, weights=weights, density=density, shift=shift, spread=spread):
                    return sum(
                        w * h * norm_cdf((c - shift * u) / spread)
                        for u, w, h in zip(nodes, weights, density)
                    )

        def exit_probability(c, continuation=continuation, mass=previous_mass):
            return mass - continuation(c)

        low, high = 0.0, 1.0
        while exit_probability(high) > increment and high < MAX_BOUNDARY:
            high *= 2.0
        if exit_probability(high) > increment:
            return _failed(looks, index, "bracket_failed", fraction)
        boundary = high
        iterations = 0
        for iteration in range(1, MAX_ITERATIONS + 1):
            midpoint = 0.5 * (low + high)
            value = exit_probability(midpoint)
            iterations = iteration
            if value > increment:
                low = midpoint
            else:
                high = midpoint
            boundary = midpoint
            if abs(value - increment) <= SOLVER_TOLERANCE:
                break
        achieved = exit_probability(boundary)
        error = abs(achieved - increment)
        status = "ok" if error <= ACCEPT_TOLERANCE else "failed"
        record = {
            "look_index": index,
            "fraction": fraction,
            "boundary": boundary if status == "ok" else None,
            "spend": {
                "increment_target": increment,
                "increment_achieved": achieved,
                "cumulative_target": target,
            },
            "solver": {
                "status": status,
                "iterations": iterations,
                "increment_error": error,
                "tolerance": ACCEPT_TOLERANCE,
                "quadrature_points": quadrature_points,
            },
        }
        looks.append(record)
        if status != "ok":
            return {
                "status": "failed",
                "failure": {
                    "look_index": index,
                    "reason": "tolerance_exceeded",
                    "fraction": fraction,
                    "error": error,
                },
                "looks": looks,
            }
        if sidedness == "two":
            grid_nodes, grid_weights = gauss_legendre(
                quadrature_points, -boundary, boundary
            )
        else:
            grid_nodes, grid_weights = gauss_legendre(
                quadrature_points, boundary - ONE_SIDED_TAIL, boundary
            )
        if grid is None:
            grid_density = [norm_pdf(x) for x in grid_nodes]
        else:
            nodes, weights, density = grid
            grid_density = [
                sum(
                    w * h * norm_pdf((x - shift * u) / spread) / spread
                    for u, w, h in zip(nodes, weights, density)
                )
                for x in grid_nodes
            ]
        mass = sum(w * h for w, h in zip(grid_weights, grid_density))
        record["spend"]["cumulative_achieved"] = 1.0 - mass
        grid = (grid_nodes, grid_weights, grid_density)
        previous_mass = mass
        previous_fraction = fraction
        cumulative_target = target
    return {
        "status": "ok",
        "looks": looks,
        "cumulative_target": cumulative_target,
        "cumulative_achieved": 1.0 - previous_mass,
        "failure": None,
    }


def _failed(looks, index, reason, fraction):
    return {
        "status": "failed",
        "failure": {"look_index": index, "reason": reason, "fraction": fraction},
        "looks": looks,
    }
