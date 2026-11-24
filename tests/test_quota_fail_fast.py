"""Un 429 « crédit épuisé » doit échouer tout de suite ; un vrai 429 reste réessayé."""
import pytest

from rag import providers


class FakeAPIError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Error code: {status} - {body}")
        self.status_code = status
        self.body = body


def test_quota_error_is_not_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr(providers.time, "sleep", sleeps.append)
    calls = []

    def fn():
        calls.append(1)
        raise FakeAPIError(429, {"error": {"type": "insufficient_quota",
                                           "code": "organization_spend_limit_exceeded",
                                           "message": "Your organization has reached its configured enforced spend limit."}})

    with pytest.raises(providers.QuotaExceeded) as info:
        providers._with_retry(fn)
    assert len(calls) == 1 and sleeps == []
    assert "Quota" in str(info.value)


def test_plain_rate_limit_is_still_retried(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise FakeAPIError(429, {"error": {"type": "rate_limit_exceeded", "message": "slow down"}})
        return "ok"

    assert providers._with_retry(fn) == "ok"
    assert len(calls) == 3
