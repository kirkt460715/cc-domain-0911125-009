import math

SQRT2 = math.sqrt(2.0)
SQRT2PI = math.sqrt(2.0 * math.pi)


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / SQRT2PI


def norm_cdf(x):
    return 0.5 * math.erfc(-x / SQRT2)


def norm_sf(x):
    return 0.5 * math.erfc(x / SQRT2)


_ACKLAM_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_ACKLAM_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)


def norm_ppf(p):
    """Inverse standard normal CDF (Acklam's method plus one Halley step)."""
    if not 0.0 < p < 1.0:
        raise ValueError("norm_ppf requires 0 < p < 1")
    low = 0.02425
    if p < low:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / (
            (((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0
        )
    elif p <= 1.0 - low:
        q = p - 0.5
        r = q * q
        x = ((((((_ACKLAM_A[0] * r + _ACKLAM_A[1]) * r + _ACKLAM_A[2]) * r + _ACKLAM_A[3]) * r + _ACKLAM_A[4]) * r + _ACKLAM_A[5]) * q) / (
            ((((_ACKLAM_B[0] * r + _ACKLAM_B[1]) * r + _ACKLAM_B[2]) * r + _ACKLAM_B[3]) * r + _ACKLAM_B[4]) * r + 1.0
        )
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / (
            (((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0
        )
    error = norm_cdf(x) - p
    update = error * SQRT2PI * math.exp(0.5 * x * x)
    return x - update / (1.0 + 0.5 * x * update)


def gauss_legendre(count, low, high):
    """Gauss-Legendre nodes and weights on [low, high]."""
    nodes = [0.0] * count
    weights = [0.0] * count
    center = 0.5 * (low + high)
    half = 0.5 * (high - low)
    for i in range((count + 1) // 2):
        z = math.cos(math.pi * (i + 0.75) / (count + 0.5))
        for _ in range(100):
            p1, p2 = 1.0, 0.0
            for j in range(1, count + 1):
                p3 = p2
                p2 = p1
                p1 = ((2.0 * j - 1.0) * z * p2 - (j - 1.0) * p3) / j
            pp = count * (z * p1 - p2) / (z * z - 1.0)
            previous = z
            z = previous - p1 / pp
            if abs(z - previous) <= 1e-15:
                break
        weight = 2.0 / ((1.0 - z * z) * pp * pp)
        nodes[i] = center - half * z
        nodes[count - 1 - i] = center + half * z
        weights[i] = half * weight
        weights[count - 1 - i] = half * weight
    return nodes, weights


def wilson_interval(successes, total, z=1.959963984540054):
    """Wilson score interval for a binomial proportion."""
    if total <= 0:
        return [0.0, 1.0]
    rate = successes / total
    denominator = 1.0 + z * z / total
    center = (rate + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt(rate * (1.0 - rate) / total + z * z / (4.0 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]
