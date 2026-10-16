"""
Final-value guardrails. Pure functions, no network, no LLM.

The final value is changed in exactly one place (ForecastBot2026.
_aggregate_predictions -> apply_final_guards, A4), and every change is written
into the Metaculus comment as "model value -> final value", so the comment
always matches what was actually submitted.
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
    Explicit formula (B8), applied only to positive ranges (lower >= 0 < upper):
        unit error  <=>  median > 10 * upper
                     or  (lower > 0 and median < lower / 10)
    e.g. 50,000 for a 0-100 question, or 3 for a 1,000-5,000 question
    (thousands vs units). Ranges that include negative numbers are not
    checked: a ratio means nothing there. CDF validity (monotonic, bounds,
    minimum step) is left to the SDK.
    """
    if not (lower >= 0 and upper > 0 and upper > lower):
        return False
    if median > 10 * upper:
        return True
    return lower > 0 and median < lower / 10
