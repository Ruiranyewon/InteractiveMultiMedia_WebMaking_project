"""OpenRouter 호출 공용 모듈: 폴백 체인, 시간 예산, 요청 속도 제한, 동시성 제한."""
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import HTTPException

log = logging.getLogger("palacewalk.llm")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 URL·헤더가 로그에 남지 않게 고정

ROOT = Path(__file__).resolve().parent.parent
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE = {429, 502, 503, 504}
BACKOFF = [2, 4]  # 라운드 사이 대기(초)
CALL_TIMEOUT = 35  # 호출 1회 상한(초)
RATE_LIMIT = 10  # IP당 분당 요청 수 (엔드포인트별)
MAX_CONCURRENT = 4  # 동시 LLM 요청 수(전역)


def load_key() -> str:
    key = os.environ.get("OPENROUTER_API", "")
    env_file = ROOT.parent / ".env"  # 프로젝트 폴더가 아닌 상위 Projects/.env를 읽는다
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line.startswith("OPENROUTER_API="):
                key = line.split("=", 1)[1].strip().strip("\"'")
    return key


API_KEY = load_key()
_hits = defaultdict(deque)
_sem = None


def _semaphore():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENT)
    return _sem


def check_rate_limit(ip: str, scope: str, limit: int = RATE_LIMIT):
    now, key = time.monotonic(), (ip, scope)
    q = _hits[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    q.append(now)
    if len(_hits) > 2000:  # 비어 있는 키 정리(메모리 무한 증가 방지)
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
            del _hits[k]


def extract_json(text, accept=None):
    """모델 응답에서 파싱되는 JSON 객체(dict)를 찾는다. accept가 있으면 그것을 통과하는 첫 객체를 반환."""
    if not isinstance(text, str):
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except ValueError:
                        break
                    if isinstance(obj, dict) and (accept is None or accept(obj) is not None):
                        return obj
                    break
        start = text.find("{", start + 1)
    return None


async def call_model(client, model, messages, parse, max_tokens, timeout):
    """모델 1회 호출. (값, 오류종류) 반환. 오류종류: transient|parse|auth|fatal."""
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.3, "messages": messages,
            "reasoning": {"effort": "low", "exclude": True}}
    t0 = time.monotonic()
    info = {"status": None, "finish": None, "tokens": None}
    err = None
    value = None
    try:
        # httpx의 timeout은 읽기 구간별이라 서버가 공백을 흘려 보내면 늘어난다. 전체 시간은 wait_for로 끊는다.
        r = await asyncio.wait_for(
            client.post(OPENROUTER_URL, json=body, headers={"Authorization": "Bearer " + API_KEY}, timeout=timeout), timeout)
        info["status"] = r.status_code
        if r.status_code in RETRYABLE:
            err = "transient"
        elif r.status_code in (401, 403):  # 키 만료·무효: 다른 모델로 넘어가도 소용없음
            err = "auth"
        elif r.status_code != 200:
            err = "fatal"
        else:
            try:
                payload = r.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict) or "error" in payload or not payload.get("choices"):
                err = "transient"
            else:
                ch = payload["choices"][0] if isinstance(payload["choices"][0], dict) else {}
                info["finish"] = ch.get("finish_reason")
                info["tokens"] = (payload.get("usage") or {}).get("total_tokens") if isinstance(payload.get("usage"), dict) else None
                content = (ch.get("message") or {}).get("content") if isinstance(ch.get("message"), dict) else None
                try:
                    value = parse(content) if isinstance(content, str) and content.strip() else None
                except (TypeError, AttributeError, ValueError, KeyError):
                    value = None
                if value is None:
                    err = "parse"
    except (httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError):
        err = "transient"
    # 키·프롬프트·본문은 남기지 않고 모델·상태·지연만 기록
    log.info("llm model=%s status=%s err=%s finish=%s tokens=%s ms=%d", model, info["status"], err,
             info["finish"], info["tokens"], (time.monotonic() - t0) * 1000)
    return (value, None) if err is None else (None, err)


async def run_chain(models, messages, parse, max_tokens=1500, budget=40):
    """모델 체인을 라운드 단위로 시도한다.

    일시 오류(429/503 등)가 난 모델은 바로 다시 두드리지 않고 다음 모델로 넘어가며,
    한 바퀴를 다 돌면 BACKOFF만큼 쉬고 다음 라운드를 시작한다. 파싱 실패가 2번 난 모델은 제외한다.
    budget(초)은 호출 타임아웃과 대기 시간에도 적용되는 하드 상한이다.
    성공 시 {"value", "model"}, 키 오류 시 {"auth_error"}, 전부 실패 시 {"errors"}.
    """
    deadline = time.monotonic() + budget
    parse_fails = defaultdict(int)
    dropped, last_err = set(), {}
    rounds = 1 + len(BACKOFF)
    async with _semaphore():
        async with httpx.AsyncClient() as client:
            for rnd in range(rounds):
                active = [m for m in models if m not in dropped]
                if not active:
                    break
                for model in active:
                    remaining = deadline - time.monotonic()
                    if remaining < 3:
                        last_err["시간 초과"] = "budget"
                        return {"errors": [f"{k}: {v}" for k, v in last_err.items()]}
                    value, err = await call_model(client, model, messages, parse, max_tokens, min(CALL_TIMEOUT, remaining - 1))
                    if value is not None:
                        return {"value": value, "model": model}
                    if err == "auth":
                        return {"auth_error": True}
                    last_err[model] = err
                    if err == "fatal":
                        dropped.add(model)
                    elif err == "parse":
                        parse_fails[model] += 1
                        if parse_fails[model] >= 2:
                            dropped.add(model)
                if rnd < rounds - 1 and len(dropped) < len(models):
                    await asyncio.sleep(max(0, min(BACKOFF[rnd], deadline - time.monotonic() - 3)))
    return {"errors": [f"{k}: {v}" for k, v in last_err.items()] or ["no models"]}
