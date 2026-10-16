import re
import subprocess
from pathlib import Path

import yaml

import main

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WF / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads `on:` as True
    return data


def evaluate_job_if(expr: str, event: str, variables: dict) -> bool:
    """Tiny evaluator for the `a == 'x' || b == 'y'` form used in our workflows."""
    def term(t: str) -> bool:
        left, right = [s.strip() for s in t.split("==")]
        value = right.strip("'")
        if left.startswith("vars."):
            return variables.get(left[5:], "") == value
        if left == "github.event_name":
            return event == value
        raise AssertionError(f"unsupported term {t}")

    return any(term(t) for t in expr.split("||"))


def test_only_two_workflows():
    assert sorted(p.name for p in WF.iterdir()) == ["test_bot.yaml", "tournament.yaml"]


def test_schedule_skipped_when_bot_enabled_unset():
    job = load("tournament.yaml")["jobs"]["forecast_job"]
    expr = job["if"]
    assert "vars.BOT_ENABLED == '1'" in expr
    assert evaluate_job_if(expr, "schedule", {}) is False
    assert evaluate_job_if(expr, "schedule", {"BOT_ENABLED": "0"}) is False
    assert evaluate_job_if(expr, "schedule", {"BOT_ENABLED": "1"}) is True
    assert evaluate_job_if(expr, "workflow_dispatch", {}) is True


def test_schedule_is_one_line_hourly_by_default():
    sched = load("tournament.yaml")["on"]["schedule"]
    assert len(sched) == 1 and sched[0]["cron"] == "17 * * * *"
    raw = (WF / "tournament.yaml").read_text()
    assert raw.count("cron:") == 1


def test_test_bot_is_manual_only():
    on = load("test_bot.yaml")["on"]
    assert list(on) == ["workflow_dispatch"]


def test_publish_comes_from_variable_and_keys_from_secrets():
    for name in ("tournament.yaml", "test_bot.yaml"):
        steps = load(name)["jobs"]["forecast_job"]["steps"]
        env = next(s for s in steps if s.get("name") == "Run bot")["env"]
        assert env["PUBLISH"] == "${{ vars.PUBLISH }}"
        for k in ("METACULUS_TOKEN", "OPENROUTER_API_KEY"):
            assert env[k] == "${{ secrets.%s }}" % k


def test_version_matches_pyproject():
    py = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', py, re.M).group(1) == main.VERSION == "0.1.0"


def test_no_keys_or_local_paths_in_tracked_files():
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    patterns = [
        re.compile(r"sk-or-v1-[0-9a-f]{20,}"),
        re.compile(r"sk-[A-Za-z0-9]{32,}"),
        re.compile(r"/Users/[a-z]"),
        re.compile(r"/home/[a-z]+/"),
        re.compile(r"/private/tmp/"),
    ]
    hits = []
    for f in files:
        if f == "poetry.lock" or f.startswith("tests/test_workflows_and_repo.py"):
            continue
        text = (ROOT / f).read_text(errors="ignore")
        hits += [f"{f}: {p.pattern}" for p in patterns if p.search(text)]
    assert hits == []


def test_model_resolution_prefers_gpt54_and_falls_back():
    models = [
        {"id": "openai/gpt-5.4", "created": 1},
        {"id": "openai/gpt-5.4-mini", "created": 1},
        {"id": "openai/gpt-5.5", "created": 2},
    ]
    assert main.resolve_models(models) == ("openai/gpt-5.4", "openai/gpt-5.4-mini")
    newer = [{"id": "openai/gpt-5.2", "created": 1}, {"id": "openai/gpt-5.6", "created": 3},
             {"id": "openai/gpt-5.6-mini", "created": 3}, {"id": "openai/gpt-5.6-pro", "created": 4}]
    assert main.resolve_models(newer) == ("openai/gpt-5.6", "openai/gpt-5.6-mini")
    assert main.resolve_models(None) == ("openai/gpt-5.4", "openai/gpt-5.4-mini")


def test_research_uses_native_search_only():
    llms = main.build_llms("openai/gpt-5.4", "openai/gpt-5.4-mini", economy=False)
    plugins = llms["researcher"].litellm_kwargs["extra_body"]["plugins"]
    assert plugins == [{"id": "web", "engine": "native"}]


def test_prompts_have_no_banned_patterns():
    src = (ROOT / "main.py").read_text()
    assert "bayes" not in src.lower()
    assert "persona" not in src.lower()
    assert "has NOT resolved yet" in src
