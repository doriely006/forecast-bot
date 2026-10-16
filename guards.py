"""
Final-value guardrails. Pure functions, no network, no LLM.

Everything the bot submits passes through here, and every change made here is
written into the Metaculus comment as "model value -> final value", so the
comment always matches what was actually submitted.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def binary_bounds() -> tuple[float, float]:
    low = _env_float("BINARY_MIN", 0.03)
    high = _env_float("BINARY_MAX", 0.97)
    if not 0.001 <= low < high <= 0.999:
        raise ValueError(f"Bad binary bounds BINARY_MIN={low} BINARY_MAX={high}")
    return low, high


def mc_floor() -> float:
    return _env_float("MC_MIN_PROB", 0.01)


@dataclass
class GuardResult:
    value: object
    note: str | None  # None = nothing changed


# ----------------------------------------------------------------- binary


def clamp_binary(p: float) -> GuardResult:
    low, high = binary_bounds()
    if math.isnan(p):
        raise ValueError("Binary prediction is NaN")
    final = min(high, max(low, p))
    if final != p:
        return GuardResult(
            final,
            f"model aggregate {p:.1%} -> final {final:.1%} "
            f"(clamped to [{low:.0%}, {high:.0%}])",
        )
    return GuardResult(final, None)


# ------------------------------------------------------- multiple choice


def floor_and_normalize(probs: dict[str, float]) -> GuardResult:
    """Every option gets at least `mc_floor()`, then everything sums to 1."""
    floor = mc_floor()
    n = len(probs)
    if n == 0:
        raise ValueError("No options")
    if floor * n >= 1:
        raise ValueError(f"MC floor {floor} too high for {n} options")
    raw = {k: max(0.0, float(v)) for k, v in probs.items()}
    total = sum(raw.values())
    if total <= 0:
        base = {k: 1.0 / n for k in raw}
    else:
        base = {k: v / total for k, v in raw.items()}
    # Water-filling: options below the floor are lifted to it, the rest share
    # the remaining mass in proportion to their size. Repeat until stable.
    # Terminates: the largest option always stays above the floor because
    # floor * n < 1.
    fixed: set[str] = set()
    while True:
        free_mass = 1.0 - floor * len(fixed)
        free_total = sum(v for k, v in base.items() if k not in fixed)
        result = {
            k: floor if k in fixed else v / free_total * free_mass
            for k, v in base.items()
        }
        newly_low = {k for k, v in result.items() if k not in fixed and v < floor}
        if not newly_low:
            break
        fixed |= newly_low
    changed = any(abs(result[k] - probs[k]) > 5e-4 for k in probs)
    note = None
    if changed:
        parts = [
            f"{k}: {probs[k]:.1%} -> {result[k]:.1%}"
            for k in probs
            if abs(result[k] - probs[k]) > 5e-4
        ]
        note = (
            f"each option floored at {floor:.0%} and renormalized; "
            + "; ".join(parts)
        )
    return GuardResult(result, note)


# ------------------------------------------------------- numeric / discrete


def implied_median(percentiles: list[tuple[float, float]]) -> float:
    """Linear interpolation of the 50th percentile from (percentile, value)."""
    pts = sorted(percentiles)
    if not pts:
        raise ValueError("No percentiles")
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if p0 <= 0.5 <= p1:
            if p1 == p0:
                return v0
            return v0 + (v1 - v0) * (0.5 - p0) / (p1 - p0)
    # 0.5 outside declared range: use the closest declared point
    return min(pts, key=lambda pv: abs(pv[0] - 0.5))[1]


def looks_like_unit_error(median: float, lower: float, upper: float) -> bool:
    """
    True when the median sits more than an order of magnitude away from the
    question's range: e.g. answering 50,000 for a 0-100 question, or 3 for a
    question whose range is 1,000-5,000 (thousands vs units).
    """
    width = upper - lower
    if width <= 0:
        return False
    distance_outside = max(lower - median, median - upper, 0.0)
    if distance_outside > 10 * width:
        return True
    if upper > 0 and median > 10 * upper:
        return True
    if lower > 0 and 0 <= median < lower / 10:
        return True
    return False


def cdf_location_to_value(
    loc: float, lower: float, upper: float, zero_point: float | None
) -> float:
    """Same scaling Metaculus uses (linear, or log when zero_point is set)."""
    if zero_point is None:
        return lower + (upper - lower) * loc
    ratio = (upper - zero_point) / (lower - zero_point)
    return lower + (upper - lower) * (ratio**loc - 1) / (ratio - 1)


def wide_fallback_percentiles(
    lower: float, upper: float, zero_point: float | None
) -> list[tuple[float, float]]:
    """
    A deliberately wide distribution: roughly uniform over the question's own
    axis. Used only when the model keeps answering in the wrong units; a wide
    honest answer scores far better than a narrow wrong one.
    """
    pts = [0.1, 0.2, 0.4, 0.6, 0.8, 0.9]
    return [(p, cdf_location_to_value(p, lower, upper, zero_point)) for p in pts]


def cdf_problems(
    cdf: list[float], open_lower: bool, open_upper: bool, cdf_size: int
) -> list[str]:
    """Checks the exact list that would be sent to Metaculus. [] = OK."""
    problems: list[str] = []
    if len(cdf) != cdf_size:
        problems.append(f"cdf has {len(cdf)} points, expected {cdf_size}")
        return problems
    if any(math.isnan(x) for x in cdf):
        return problems + ["cdf contains NaN"]
    steps = [b - a for a, b in zip(cdf, cdf[1:])]
    if any(s < 0 for s in steps):
        problems.append("cdf is not monotonic")
    min_step = 0.01 / (cdf_size - 1) * 0.5
    if min(steps) < min_step:
        problems.append(f"cdf step {min(steps):.2e} below minimum {min_step:.2e}")
    max_step = 0.2 * 200 / (cdf_size - 1)
    if max(steps) > max_step + 1e-9:
        problems.append(f"distribution too narrow (bucket mass {max(steps):.3f})")
    if not open_lower and abs(cdf[0]) > 1e-9:
        problems.append("closed lower bound but cdf[0] != 0")
    if open_lower and cdf[0] < 0.001 - 1e-9:
        problems.append("open lower bound needs >= 0.1% below range")
    if not open_upper and abs(cdf[-1] - 1) > 1e-9:
        problems.append("closed upper bound but cdf[-1] != 1")
    if open_upper and cdf[-1] > 0.999 + 1e-9:
        problems.append("open upper bound needs >= 0.1% above range")
    return problems
