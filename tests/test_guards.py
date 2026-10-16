import guards
import pytest


def test_binary_clamped_both_ends():
    hi = guards.clamp_binary(0.995)
    lo = guards.clamp_binary(0.001)
    assert hi.value == 0.97 and "99.5% -> final 97.0%" in hi.note
    assert lo.value == 0.03 and "final 3.0%" in lo.note


def test_binary_untouched_inside_bounds():
    r = guards.clamp_binary(0.42)
    assert r.value == 0.42 and r.note is None


def test_binary_bounds_from_env(monkeypatch):
    monkeypatch.setenv("BINARY_MIN", "0.05")
    monkeypatch.setenv("BINARY_MAX", "0.9")
    assert guards.clamp_binary(0.99).value == 0.9
    assert guards.clamp_binary(0.0).value == 0.05


def test_binary_bad_env_rejected(monkeypatch):
    monkeypatch.setenv("BINARY_MIN", "0.8")
    monkeypatch.setenv("BINARY_MAX", "0.2")
    with pytest.raises(ValueError):
        guards.clamp_binary(0.5)


@pytest.mark.parametrize(
    "probs",
    [
        {"a": 0.0, "b": 0.0, "c": 1.0},
        {"a": 0.7, "b": 0.3, "c": 0.0},
        {"a": 0.2, "b": 0.2, "c": 0.2},  # does not sum to 1
        {f"o{i}": (1.0 if i == 0 else 0.0) for i in range(40)},
    ],
)
def test_mc_floor_and_normalize(probs):
    r = guards.floor_and_normalize(probs)
    assert abs(sum(r.value.values()) - 1) < 1e-9
    assert all(v >= 0.01 - 1e-12 for v in r.value.values())
    assert set(r.value) == set(probs)


def test_mc_untouched_when_fine():
    r = guards.floor_and_normalize({"a": 0.5, "b": 0.3, "c": 0.2})
    assert r.note is None


@pytest.mark.parametrize(
    "median,lo,hi,expected",
    [
        (50_000, 0, 100, True),  # 1000x too big (answered in wrong units)
        (50, 0, 100, False),
        (3, 1000, 5000, True),  # 1000x too small
        (3000, 1000, 5000, False),
        (3_000_000, 1000, 5000, True),
        (0.45, 0, 100, False),  # small but inside the range: not flagged
        (45, 0, 1, True),  # percent given for a fraction question
        (-500, -10, 10, True),
        (12, -10, 10, False),
    ],
)
def test_unit_error_detection(median, lo, hi, expected):
    assert guards.looks_like_unit_error(median, lo, hi) is expected


def test_implied_median_interpolates():
    pts = [(0.1, 10), (0.4, 40), (0.6, 60), (0.9, 90)]
    assert guards.implied_median(pts) == pytest.approx(50)


def test_wide_fallback_spans_range_linear_and_log():
    lin = guards.wide_fallback_percentiles(0, 100, None)
    assert [v for _, v in lin] == pytest.approx([10, 20, 40, 60, 80, 90])
    log = guards.wide_fallback_percentiles(1000, 5000, 0)
    vals = [v for _, v in log]
    assert vals == sorted(vals) and 1000 < vals[0] < vals[-1] < 5000


def test_cdf_problems_detects_bad_cdfs():
    n = 201
    good = [0.001 + 0.998 * i / (n - 1) for i in range(n)]
    assert guards.cdf_problems(good, True, True, n) == []
    bad = list(good)
    bad[100], bad[101] = bad[101], bad[100]
    assert any("monotonic" in p for p in guards.cdf_problems(bad, True, True, n))
    assert guards.cdf_problems(good, False, True, n)  # closed lower needs cdf[0]=0
    spike = [0.0] * 100 + [1.0] * 101
    assert any("narrow" in p for p in guards.cdf_problems(spike, False, False, n))
    assert guards.cdf_problems(good[:-1], True, True, n)
