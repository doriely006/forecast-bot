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
        (0, 1000, 5000, True),  # zero for a 1,000-5,000 question
        (-500, -10, 10, False),  # range includes negatives: no ratio check (B8)
        (12, -10, 10, False),
        (1001, 0, 100, True),  # just over 10x the upper bound
        (999, 0, 100, False),
    ],
)
def test_unit_error_detection(median, lo, hi, expected):
    assert guards.looks_like_unit_error(median, lo, hi) is expected


def test_implied_median_interpolates():
    pts = [(0.1, 10), (0.4, 40), (0.6, 60), (0.9, 90)]
    assert guards.implied_median(pts) == pytest.approx(50)


def test_mc_floor_is_iterative_many_tiny_options():  # B13
    # One pass of "lift to floor then renormalize" would push lifted options
    # back under the floor; the iterative version must not.
    probs = {"big": 0.9, **{f"o{i}": 0.004 for i in range(25)}}
    r = guards.floor_and_normalize(probs)
    assert abs(sum(r.value.values()) - 1) < 1e-12
    assert min(r.value.values()) >= 0.01 - 1e-12
    assert r.value["big"] == pytest.approx(1 - 0.01 * 25)


def test_mc_floor_too_high_rejected(monkeypatch):
    monkeypatch.setenv("MC_MIN_PROB", "0.3")
    with pytest.raises(ValueError):
        guards.floor_and_normalize({"a": 0.5, "b": 0.3, "c": 0.1, "d": 0.1})


def _numeric_question(**kw):
    from forecasting_tools import NumericQuestion

    base = dict(question_text="q", id_of_post=1, id_of_question=1, page_url="https://example.invalid/q/1/",
                lower_bound=0, upper_bound=100, open_lower_bound=False, open_upper_bound=True)
    base.update(kw)
    return NumericQuestion(**base)


def test_sdk_cdf_is_monotonic_and_respects_bounds():  # B8: validity is the SDK's job
    from forecasting_tools import NumericDistribution, Percentile

    q = _numeric_question()
    pts = [Percentile(percentile=p, value=v) for p, v in [(0.1, 5), (0.2, 6), (0.4, 6.5), (0.6, 7), (0.8, 7.2), (0.9, 7.3)]]
    cdf = [p.percentile for p in NumericDistribution.from_question(pts, q).get_cdf()]
    assert len(cdf) == 201 and cdf[0] == 0.0 and cdf[-1] <= 0.999 + 1e-9
    steps = [b - a for a, b in zip(cdf, cdf[1:])]
    assert min(steps) >= 0.01 / 200 * 0.99  # SDK enforces a minimum step


def test_sdk_rejects_non_monotonic_percentiles():  # B8
    from forecasting_tools import NumericDistribution, Percentile

    q = _numeric_question()
    pts = [Percentile(percentile=p, value=v) for p, v in [(0.1, 50), (0.2, 40), (0.4, 60), (0.6, 70), (0.8, 80), (0.9, 90)]]
    with pytest.raises(Exception):
        NumericDistribution.from_question(pts, q).get_cdf()
