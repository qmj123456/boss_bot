from shared.recruiter_job_catalog import _fetch_via_browser


class FakePage:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.evaluate_calls = 0
        self.wait_calls = 0

    def evaluate(self, script, args):
        self.evaluate_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def wait_for_load_state(self, state, timeout):
        self.wait_calls += 1


def test_fetch_via_browser_retries_when_navigation_destroys_context():
    page = FakePage([
        RuntimeError("Page.evaluate: Execution context was destroyed, most likely because of a navigation."),
        {"ok": True, "status": 200, "body": "{}"},
    ])

    result = _fetch_via_browser(page, "https://www.zhipin.com/test")

    assert result == {"ok": True, "status": 200, "body": "{}"}
    assert page.evaluate_calls == 2
    assert page.wait_calls == 1


def test_fetch_via_browser_does_not_retry_non_navigation_errors():
    page = FakePage([RuntimeError("Page.evaluate: TypeError: failed to fetch")])

    result = _fetch_via_browser(page, "https://www.zhipin.com/test")

    assert result["ok"] is False
    assert "failed to fetch" in result["error"]
    assert page.evaluate_calls == 1
    assert page.wait_calls == 0
