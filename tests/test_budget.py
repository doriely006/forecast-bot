import budget


def info(limit, usage):
    return budget.parse_key_payload(
        {"data": {"limit": limit, "usage": usage, "limit_remaining": None if limit is None else limit - usage}}
    )


def test_three_states():
    assert budget.decide(info(100, 10), {}).state == "normal"
    assert budget.decide(info(100, 90), {}).state == "economy"  # $10 left
    stop = budget.decide(info(100, 98), {})  # $2 left
    assert stop.state == "stop" and not stop.may_call_llm and stop.red


def test_thresholds_from_env():
    env = {"BUDGET_ECONOMY_BELOW": "50", "BUDGET_STOP_BELOW": "20"}
    assert budget.decide(info(100, 60), env).state == "economy"
    assert budget.decide(info(100, 85), env).state == "stop"


def test_unlimited_key_refused_unless_allowed():
    d = budget.decide(info(None, 5), {})
    assert d.state == "refuse_unlimited" and d.red
    assert budget.decide(info(None, 5), {"ALLOW_UNLIMITED_KEY": "1"}).state == "normal"


def test_unknown_key_info_is_a_stop():
    d = budget.decide(None, {})
    assert d.state == "unknown" and not d.may_call_llm


class _Resp:
    def __init__(self, payload, ok=True):
        self.payload, self.ok = payload, ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http 401")

    def json(self):
        return self.payload


def test_fetch_key_info_parses_and_sends_bearer():
    seen = {}

    def get(url, headers, timeout):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return _Resp({"data": {"limit": 50, "usage": 12.5, "limit_remaining": 37.5}})

    ki = budget.fetch_key_info("test-key-not-real", get=get)
    assert ki.remaining == 37.5 and ki.limit == 50
    assert seen["url"] == budget.OPENROUTER_KEY_URL and seen["auth"] == "Bearer test-key-not-real"


def test_fetch_key_info_failure_and_missing_key():
    assert budget.fetch_key_info(None) is None
    assert budget.fetch_key_info("x", get=lambda *a, **k: _Resp({}, ok=False)) is None
