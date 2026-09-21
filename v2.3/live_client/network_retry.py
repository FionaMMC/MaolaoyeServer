"""Bounded retries for reading frozen server data; never retry QMT submissions."""

from __future__ import annotations

import time

import requests


def get_with_retry(url, *, attempts=3, sleep=time.sleep, **kwargs):
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            response = requests.get(url, **kwargs)
            # Retry only explicitly transient HTTP failures, not authentication,
            # business errors, malformed JSON, or an empty valid order batch.
            if response.status_code in {429, 502, 503, 504} and attempt + 1 < attempts:
                response.close()
            else:
                response.raise_for_status()
                return response
        except (requests.Timeout, requests.ConnectionError):
            if attempt + 1 == attempts:
                raise
        sleep(2**attempt)
    raise AssertionError("unreachable")
