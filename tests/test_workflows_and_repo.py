import re
import subprocess
import sys
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
    """Tiny evaluator for the `a == 'x' || b != ''` form used in our workflows."""
    def term(t: str) -> bool:
        op = "!=" if "!=" in t else "=="
        left, right = [s.strip() for s in t.split(op)]
        value = right.strip("'")
        if left.startswith("vars."):
            actual = variables.get(left[5:], "")
        elif left == "github.event_name":
            actual = event
        else:
            raise AssertionError(f"unsupported term {t}")
        return (actual == value) if op == "==" else (actual != value)

    return any(term(t) for t in expr.split("||"))


def test_only_two_workflows():
    assert sorted(p.name for p in WF.iterdir()) == ["test_bot.yaml", "tournament.yaml"]


def test_schedule_skipped_when_bot_enabled_unset():
    job = load("tournament.yaml")["jobs"]["forecast_job"]
    expr = job["if"]
    assert evaluate_job_if(expr, "schedule", {}) is False
    # any other non-empty value starts the job so precheck.py can FAIL it with the reason (B5)
    assert evaluate_job_if(expr, "schedule", {"BOT_ENABLED": "0"}) is True
    assert evaluate_job_if(expr, "schedule", {"BOT_ENABLED": "1"}) is True
    assert evaluate_job_if(expr, "workflow_dispatch", {}) is True


def test_schedule_every_20_minutes_never_on_the_hour():  # override 1, A5
    sched = load("tournament.yaml")["on"]["schedule"]
    assert len(sched) == 1 and sched[0]["cron"] == "7,27,47 * * * *"
    raw = (WF / "tournament.yaml").read_text()
    assert raw.count("cron:") == 1


def test_test_bot_is_manual_only():
    on = load("test_bot.yaml")["on"]
    assert list(on) == ["workflow_dispatch"]


def _run_env(name):
    job = load(name)["jobs"]["forecast_job"]
    step = next(s for s in job["steps"] if s.get("name") == "Run bot")
    return {**job.get("env", {}), **step["env"]}


def test_stop_loss_variables_reach_both_steps():  # v0.1.3
    job = load("tournament.yaml")["jobs"]["forecast_job"]
    for k in ("MAX_QUESTION_FAILED_RUNS", "SKIP_COOLDOWN_HOURS", "MIN_MINUTES_BEFORE_CLOSE"):
        assert job["env"][k] == "${{ vars.%s }}" % k  # job-level: the Quick check step and Run bot both see it


def test_publish_comes_from_variable_and_keys_from_secrets():
    for name in ("tournament.yaml", "test_bot.yaml"):
        env = _run_env(name)
        assert env["PUBLISH"] == "${{ vars.PUBLISH }}"
        for k in ("METACULUS_TOKEN", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
            assert env[k] == "${{ secrets.%s }}" % k
        assert not [k for k in env if "EXA" in k or "PERPLEXITY" in k]  # B10


def test_version_matches_pyproject():
    import precheck

    py = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', py, re.M).group(1)
    assert version == main.VERSION == precheck.VERSION == "0.3.1"


def test_workflows_locked_down():  # B6, B10, override 3
    for name in ("tournament.yaml", "test_bot.yaml"):
        wf = load(name)
        raw = (WF / name).read_text()
        assert wf["permissions"] == {"contents": "read"}
        assert set(wf["on"]) <= {"workflow_dispatch", "schedule"}
        assert "pull_request" not in raw
        assert wf["concurrency"]["cancel-in-progress"] is False
        job = wf["jobs"]["forecast_job"]
        assert job["timeout-minutes"] == 40
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_idle_run_skips_heavy_steps():  # B14
    steps = load("tournament.yaml")["jobs"]["forecast_job"]["steps"]
    names = [s["name"] for s in steps]
    assert names.index("Quick check") < names.index("Set up Python")
    assert next(s for s in steps if s["name"] == "Quick check")["run"] == "python3 precheck.py"
    for s in steps:
        if s["name"] in ("Set up Python", "Load cached venv", "Install poetry (cache miss only)",
                         "Install dependencies (cache miss only)", "Run bot"):
            assert "steps.precheck.outputs.work == 'true'" in s["if"], s["name"]


def test_light_modules_need_no_installed_packages():  # B14
    code = (
        "import sys; import precheck, standings, budget, state; "
        "bad = [m for m in ('forecasting_tools', 'requests', 'litellm', 'numpy') if m in sys.modules]; "
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


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


def test_research_uses_native_search_only():
    llms = main.build_llms("openai/gpt-5.6-terra", "openai/gpt-5.4-mini")
    plugins = llms["researcher"].litellm_kwargs["extra_body"]["plugins"]
    assert plugins == [{"id": "web", "engine": "native"}]  # B4
    assert llms["researcher"].model == "openrouter/openai/gpt-5.4-mini"  # C2
    assert llms["default"].model == "openrouter/openai/gpt-5.6-terra"
    assert not re.search(r"(\}|[\w.-]):online", (ROOT / "main.py").read_text())  # no ":online" model IDs


def test_prompts_have_no_banned_patterns():
    src = (ROOT / "main.py").read_text()
    assert "bayes" not in src.lower()
    assert "persona" not in src.lower()
    assert "has NOT resolved yet" in src
