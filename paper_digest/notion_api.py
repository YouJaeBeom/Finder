"""The Notion transport layer: throttled, retried, and honest about failures.

Every Notion call in this tool goes through :func:`request`. That matters more
for a lab than it did for one person. A single member's digest is ~20 pages and
never came near Notion's limits; ten members writing in one run is 200+ requests
against a published average of three per second, and the old code raised on the
first 429 — losing every page not yet written.

Two mechanisms, and they solve different halves of the problem:

* **Throttle.** A minimum gap between requests, so a run stays under the limit
  instead of discovering it. This is per process, which is the reason the
  pipeline processes members sequentially in one job rather than in parallel
  GitHub Actions jobs — the rate limit is per *token*, so parallel jobs share
  one budget while each believes it has the whole thing.
* **Retry.** A 429 means "later", not "never". ``Retry-After`` is honoured when
  Notion sends it, and 5xx and dropped connections are retried the same way.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

STATE_FILE = "state.json"

# Notion documents ~3 requests/second averaged over time. 0.34s is that rate
# with no headroom spent on being clever; page writes dominate a run and 300 of
# them cost 100 seconds, which is nothing next to the note generation they
# follow.
MIN_REQUEST_INTERVAL = 0.34

MAX_ATTEMPTS = 5
BACKOFF_BASE = 2.0

_last_request_at = 0.0


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def check(resp, what: str) -> None:
    """Raise on an error response, quoting what Notion actually said.

    ``raise_for_status`` alone yields "401 Client Error: Unauthorized", which
    tells a user nothing. Notion's body carries the sentence that matters — for
    example "Make sure the relevant pages and databases are shared with your
    integration" — and that is the difference between a fixable error and a
    baffling one.
    """
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        try:
            detail = resp.json().get("message") or resp.text[:300]
        except Exception:
            detail = (resp.text or "")[:300]
        raise RuntimeError(f"Notion {what} failed: {detail}") from exc


def _retry_delay(resp, attempt: int) -> float:
    """How long to wait before retrying, preferring Notion's own instruction."""
    raw = (resp.headers or {}).get("Retry-After") if hasattr(resp, "headers") else None
    try:
        if raw is not None:
            return max(float(raw), MIN_REQUEST_INTERVAL)
    except (TypeError, ValueError):
        pass
    return BACKOFF_BASE * (attempt + 1)


def _throttle() -> None:
    global _last_request_at
    gap = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if gap > 0:
        time.sleep(gap)


def request(
    method: str,
    path: str,
    token: str,
    *,
    what: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: int = 30,
):
    """One Notion request, throttled and retried. Returns the final response.

    Retryable outcomes (429, 5xx, a dropped connection) are waited out and tried
    again. Anything else — including 4xx that will never succeed — comes back as
    it is, for the caller to hand to :func:`check`, which quotes Notion's own
    error message. Only exhausting the retries on a transport error raises here.

    ``getattr(requests, method)`` is resolved per call rather than bound at
    import, so tests that patch ``requests.get`` / ``requests.post`` on this
    module still intercept it.
    """
    global _last_request_at

    url = f"{NOTION_BASE_URL}{path}"
    resp = None
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_ATTEMPTS):
        _throttle()
        try:
            resp = getattr(requests, method)(
                url,
                headers=headers(token),
                timeout=timeout,
                **({"json": json_body} if json_body is not None else {}),
                **({"params": params} if params is not None else {}),
            )
        except requests.RequestException as exc:
            _last_request_at = time.monotonic()
            last_exc = exc
            resp = None
            delay = BACKOFF_BASE * (attempt + 1)
            logger.info("Notion %s failed to connect (%s) — retrying in %.0fs "
                        "(attempt %d/%d)", what, exc, delay, attempt + 1,
                        MAX_ATTEMPTS)
            time.sleep(delay)
            continue

        _last_request_at = time.monotonic()
        status = resp.status_code
        if status == 429 or status >= 500:
            delay = _retry_delay(resp, attempt)
            logger.info("Notion returned %d for %s — waiting %.1fs "
                        "(attempt %d/%d)", status, what, delay, attempt + 1,
                        MAX_ATTEMPTS)
            time.sleep(delay)
            continue

        return resp

    if resp is None:
        raise RuntimeError(
            f"Notion {what} failed after {MAX_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    # Out of retries on a 429 or 5xx. Returned rather than raised so `check`
    # reports it in the same shape as every other Notion failure.
    logger.warning("Notion %s still failing after %d attempts", what, MAX_ATTEMPTS)
    return resp


# ── Local state: a cache of Notion coordinates, never a source of truth ────────

def load_state() -> dict:
    """The cached Notion IDs, or {} when there is no usable cache.

    Losing this file costs a lookup, never a duplicate: every resolver falls
    back to finding its page or database by title under the parent. That
    property is what makes the file safe to leave uncommitted on a failed run.
    """
    p = Path(STATE_FILE)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    Path(STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
