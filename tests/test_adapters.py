"""
GeminiAdapter retry-logic test suite.

The suite runs with no network and no key (see the module's own docstring) - every
network boundary here is mocked. What IS real and worth testing offline: the retry
control flow itself is deterministic, so a 429 that resolves on the second attempt, or
never resolves, must behave predictably regardless of what the live API actually does.
"""

import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch

from data_plane.adapters import Credentials, GeminiAdapter, ModelCallError


def _http_error(code: int, retry_after: str = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(url="https://example", code=code, msg="err",
                                  hdrs=headers, fp=None)


class _FakeResponse:
    """Minimal stand-in for the context manager `urlopen()` returns."""

    def __init__(self, body: dict):
        self._buf = BytesIO(json.dumps(body).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._buf.read()


CLEAN_BODY = {
    "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
}


class TestRetryOnTransientErrors(unittest.TestCase):

    def _adapter(self):
        return GeminiAdapter(max_retries=3, backoff_s=0.001)   # fast in tests; sleep is mocked anyway

    def test_succeeds_after_one_429(self):
        calls = [_http_error(429), _FakeResponse(CLEAN_BODY)]

        def fake_urlopen(*a, **kw):
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch("data_plane.adapters.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("data_plane.adapters.time.sleep") as sleep_mock:
            r = self._adapter().generate("sys", [{"role": "user", "content": "hi"}],
                                         Credentials(provider="gemini", model="m", api_key="k"))
        self.assertEqual(r.text, "hi")
        sleep_mock.assert_called_once()

    def test_retry_after_header_is_honored(self):
        calls = [_http_error(429, retry_after="7"), _FakeResponse(CLEAN_BODY)]

        def fake_urlopen(*a, **kw):
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch("data_plane.adapters.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("data_plane.adapters.time.sleep") as sleep_mock:
            self._adapter().generate("sys", [{"role": "user", "content": "hi"}],
                                     Credentials(provider="gemini", model="m", api_key="k"))
        sleep_mock.assert_called_once_with(7.0)

    def test_exhausts_retries_and_raises(self):
        with patch("data_plane.adapters.urllib.request.urlopen",
                   side_effect=lambda *a, **kw: (_ for _ in ()).throw(_http_error(503))), \
             patch("data_plane.adapters.time.sleep") as sleep_mock:
            with self.assertRaises(ModelCallError) as ctx:
                self._adapter().generate("sys", [{"role": "user", "content": "hi"}],
                                         Credentials(provider="gemini", model="m", api_key="k"))
        self.assertIn("503", str(ctx.exception))
        self.assertEqual(sleep_mock.call_count, 3)   # max_retries, not max_retries + 1

    def test_non_retryable_code_fails_immediately(self):
        with patch("data_plane.adapters.urllib.request.urlopen",
                   side_effect=lambda *a, **kw: (_ for _ in ()).throw(_http_error(404))), \
             patch("data_plane.adapters.time.sleep") as sleep_mock:
            with self.assertRaises(ModelCallError) as ctx:
                self._adapter().generate("sys", [{"role": "user", "content": "hi"}],
                                         Credentials(provider="gemini", model="m", api_key="k"))
        self.assertIn("404", str(ctx.exception))
        sleep_mock.assert_not_called()

    def test_no_api_key_raises_without_any_call(self):
        with patch("data_plane.adapters.urllib.request.urlopen") as urlopen_mock:
            with self.assertRaises(ModelCallError):
                GeminiAdapter().generate("sys", [{"role": "user", "content": "hi"}],
                                         Credentials(provider="gemini", model="m", api_key=""))
        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
