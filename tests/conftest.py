"""
Offline test harness: no network (sockets are blocked), fake LLM, fake
Metaculus client, fixture questions.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
for _k in ("METACULUS_TOKEN", "OPENROUTER_API_KEY", "ASKNEWS_CLIENT_ID", "ASKNEWS_SECRET", "PUBLISH",
           "ANTHROPIC_API_KEY", "LLM_PROVIDER", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("tests must not touch the network")


socket.socket.connect = _blocked  # type: ignore[method-assign]
socket.create_connection = _blocked  # type: ignore[assignment]

FIXTURES = ROOT / "tests" / "fixtures" / "questions.json"


def load_fixture_questions() -> dict[str, list]:
    from forecasting_tools import BinaryQuestion, MultipleChoiceQuestion, NumericQuestion
    from forecasting_tools.data_models.questions import DiscreteQuestion

    classes = {
        "binary": BinaryQuestion,
        "multiple_choice": MultipleChoiceQuestion,
        "numeric": NumericQuestion,
        "discrete": DiscreteQuestion,
    }
    raw = json.loads(FIXTURES.read_text())
    out: dict[str, list] = {}
    for tournament, items in raw.items():
        qs = []
        for item in items:
            item = dict(item)
            cls = classes[item.pop("type")]
            item.setdefault("page_url", f"https://example.invalid/questions/{item['id_of_post']}/")
            item.setdefault("resolution_criteria", "fixture criteria")
            item.setdefault("background_info", "fixture background")
            item.setdefault("fine_print", "")
            qs.append(cls(**item))
        out[tournament] = qs
    return out


class FakeClient:
    """Stands in for forecasting_tools.MetaculusClient."""

    def __init__(self, questions: dict[str, list] | None = None, fail_fetch: bool = False,
                 fail_tournaments: tuple = ()):
        self.questions = questions if questions is not None else load_fixture_questions()
        self.fail_fetch = fail_fetch
        self.fail_tournaments = set(fail_tournaments)
        self.predictions: list[tuple[str, int, object]] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_comment_once: set[int] = set()
        self.fail_comment_always: set[int] = set()

    async def get_questions_matching_filter(self, api_filter, num_questions=None, **kw):
        if self.fail_fetch:
            raise ConnectionError("fake fetch failure")
        (tid,) = api_filter.allowed_tournaments
        if tid in self.fail_tournaments:
            raise ConnectionError(f"fake fetch failure for {tid}")
        return [q.model_copy(deep=True) for q in self.questions.get(tid, [])]

    def post_binary_question_prediction(self, qid, p):
        self.predictions.append(("binary", qid, p))

    def post_multiple_choice_question_prediction(self, qid, probs):
        self.predictions.append(("mc", qid, probs))

    def post_numeric_question_prediction(self, qid, cdf):
        self.predictions.append(("numeric", qid, cdf))

    def post_question_comment(self, post_id, text, is_private=True, included_forecast=True):
        if post_id in self.fail_comment_always:
            raise ConnectionError("fake comment outage")
        if post_id in self.fail_comment_once:
            self.fail_comment_once.discard(post_id)
            raise ConnectionError("fake comment failure")
        self.comments.append((post_id, text))


TOURNAMENTS = [("FutureEval Fall 2026", "FutureEval Fall 2026"), ("MiniBench", "MiniBench")]


def _percentile_answer(values: list[float]) -> str:
    ps = [10, 20, 40, 60, 80, 90]
    return "\n".join(f"Percentile {p}: {v:f}" for p, v in zip(ps, values))


class FakeOpenRouterError(Exception):
    """Shaped like litellm's errors: carries an HTTP status_code."""

    def __init__(self, status_code: int):
        super().__init__(f"OpenrouterException - fake error {status_code}")
        self.status_code = status_code


class FakeLlmState:
    def __init__(self):
        self.calls: list[str] = []
        self.flaky_seen = False


@pytest.fixture
def fake_llm(monkeypatch):
    """Replaces every LLM call (GeneralLlm.invoke) and the output parser."""
    import main
    from forecasting_tools import BinaryPrediction, GeneralLlm, Percentile, PredictedOptionList
    from forecasting_tools.data_models.multiple_choice_report import PredictedOption

    state = FakeLlmState()

    async def fake_invoke(self, prompt, system_prompt=None):
        text = prompt if isinstance(prompt, str) else str(prompt)
        state.calls.append(text)
        if "PAYMENT402" in text:
            raise FakeOpenRouterError(402)
        if "ALWAYSFAILS" in text:
            raise RuntimeError("fake LLM outage for this question")
        if "FLAKY" in text and not state.flaky_seen:
            state.flaky_seen = True
            raise RuntimeError("fake transient LLM error")
        if "Please summarize the following research" in text:
            return "Fake research summary."
        if "research assistant" in text:
            return "Fake research: nothing decisive yet. Base rate about 30%."
        if "Percentile 10" in text and "YYYY" not in text:
            retry = "Your previous answer was" in text
            if "UNITBUG twice" in text:
                return "Outside view... " + _percentile_answer([1.5e6, 2e6, 2.5e6, 3e6, 3.5e6, 4.5e6])
            if "UNITBUG once" in text and not retry:
                return "Outside view... " + _percentile_answer([10000, 20000, 40000, 60000, 80000, 90000])
            if "goals" in text:
                return "Outside view... " + _percentile_answer([1, 2, 3, 4, 6, 8])
            return "Outside view... " + _percentile_answer([10, 20, 40, 60, 80, 90])
        if "The options are" in text:
            return "Outside view...\nAlpha: 70%\nBeta: 30%\nGamma: 0%"
        if "model says 99%" in text:
            return "Outside view... Inside view...\nProbability: 99%"
        return "Outside view... Inside view...\nProbability: 40%"

    async def fake_structure_output(text_to_structure, output_type, model=None,
                                    num_validation_samples=1, allowed_tries=3,
                                    additional_instructions=None):
        text = text_to_structure
        if output_type is BinaryPrediction:
            p = float(re.findall(r"Probability: (\d+(?:\.\d+)?)%", text)[-1]) / 100
            return BinaryPrediction(prediction_in_decimal=p)
        if output_type is PredictedOptionList:
            opts = re.findall(r"^(\w+): (\d+(?:\.\d+)?)%$", text, flags=re.M)
            return PredictedOptionList(
                predicted_options=[PredictedOption(option_name=n, probability=float(v) / 100) for n, v in opts]
            )
        if "Percentile" in str(output_type):
            pairs = re.findall(r"Percentile (\d+): ([\d.]+)", text)[-6:]
            return [Percentile(percentile=int(p) / 100, value=float(v)) for p, v in pairs]
        raise AssertionError(f"unexpected output type {output_type}")

    monkeypatch.setattr(GeneralLlm, "invoke", fake_invoke)
    monkeypatch.setattr(main, "structure_output", fake_structure_output)
    return state
