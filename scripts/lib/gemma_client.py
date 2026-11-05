"""Minimal client for the self-hosted Gemma (OpenAI-compatible vLLM) endpoint.

Standard library only (`.venv` carries no `requests`/`httpx`/`openai`). Config comes
from `lib.config.gemma_config()` -- URL, model and key are never defaulted or hardcoded.

Design points, each from a failure seen on this endpoint family:
* temperature 0 and an explicit `max_tokens`: the model is served with a reasoning
  parser, so the budget must leave headroom for hidden reasoning before the JSON.
* Output is *parsed defensively*: code fences, leading prose and a trailing comma are
  tolerated, but anything that still is not valid JSON counts as a failure and is
  retried once with a stricter reminder (same prompt would reproduce the same output
  at temperature 0, so a retry has to change the prompt).
* Every call returns an `ok` flag and the failure reason; callers decide what to store.
  Failures are never silently turned into empty results.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from .config import gemma_config

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def endpoint_host() -> str:
    """Host the client will talk to, for the startup log (a past run hit the wrong one)."""
    return urlparse(gemma_config()["url"]).netloc


def parse_json_object(text: str) -> Optional[dict]:
    """Extract the first JSON object from a completion; None if there is none."""
    if not text:
        return None
    t = _FENCE.sub("", text.strip())
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = _TRAILING_COMMA.sub(r"\1", t[start:i + 1])
                try:
                    obj = json.loads(blob)
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _post(payload: dict, cfg: dict) -> dict:
    req = urllib.request.Request(
        f"{cfg['url']}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
        return json.loads(r.read().decode("utf-8"))


def chat_json(system: str, user: str, *, max_tokens: int = 1200,
              validate: Optional[Callable[[dict], Optional[str]]] = None,
              retries: int = 1) -> dict:
    """One chat completion that must return a JSON object.

    `validate(obj)` returns None if the object is acceptable, else a short reason.
    Returns {ok, obj, error, tokens, latency_s, attempts}.
    """
    cfg = gemma_config()
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    t0 = time.time()
    tokens, err = 0, "no attempt"
    for attempt in range(1, retries + 2):
        payload = {"model": cfg["model"], "messages": msgs, "temperature": 0,
                   "max_tokens": max_tokens,
                   "response_format": {"type": "json_object"}}
        try:
            resp = _post(payload, cfg)
        except urllib.error.HTTPError as e:
            err = f"http_{e.code}"
            if e.code in (401, 403):          # auth will not fix itself on retry
                break
            time.sleep(min(2 ** attempt, 10))
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            err = f"net_{type(e).__name__}"
            time.sleep(min(2 ** attempt, 10))
            continue
        tokens += int((resp.get("usage") or {}).get("total_tokens") or 0)
        choice = (resp.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        obj = parse_json_object(text)
        if obj is None:
            err = "unparseable_json" + ("_truncated" if choice.get("finish_reason") == "length" else "")
        else:
            bad = validate(obj) if validate else None
            if bad is None:
                return {"ok": True, "obj": obj, "error": None, "tokens": tokens,
                        "latency_s": round(time.time() - t0, 2), "attempts": attempt}
            err = f"invalid:{bad}"
        # A retry at temperature 0 repeats itself unless the prompt changes.
        msgs = msgs[:2] + [{"role": "user",
                            "content": f"Your previous answer was rejected ({err}). Reply with "
                                       "ONLY one valid JSON object that follows the schema exactly."}]
    return {"ok": False, "obj": None, "error": err, "tokens": tokens,
            "latency_s": round(time.time() - t0, 2), "attempts": retries + 1}


def run_batch(items: Iterable, fn: Callable, concurrency: Optional[int] = None) -> list:
    """Apply `fn` to every item concurrently, preserving input order."""
    workers = concurrency or gemma_config()["concurrency"]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(fn, items))
