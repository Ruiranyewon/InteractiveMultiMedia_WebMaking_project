"""궁궐 산책 추천 — 1단계(마음·몸·취향 진단) + 2단계(장소 추천) 백엔드."""
import hashlib
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import assess, auth, db, llm, recommend, saved

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
STATIC = Path(__file__).resolve().parent.parent / "static"
TEXT_MODELS = os.environ.get(
    "TEXT_MODELS",
    "google/gemma-4-26b-a4b-it:free,google/gemma-4-31b-it:free,nvidia/nemotron-3-super-120b-a12b:free",
).split(",")
MAX_BODY = 8 * 1024
CSP = ("default-src 'self'; img-src 'self' https://upload.wikimedia.org https://thumb.wikimedia.org data:; style-src 'self'; "
       "script-src 'self'; frame-ancestors 'none'; base-uri 'none'")

# 문서 페이지는 공개하지 않는다.
app = FastAPI(title="PalaceWalk", docs_url=None, redoc_url=None, openapi_url=None)
db.init_db()
app.include_router(auth.router)
app.include_router(saved.router)

_cache: "OrderedDict[str, dict]" = OrderedDict()  # 같은 응답 → 같은 해석(무료 쿼터 절약), 최근 200건


def cache_get(key):
    if key in _cache:
        _cache.move_to_end(key)
        return json.loads(json.dumps(_cache[key]))
    return None


def cache_put(key, value):
    _cache[key] = value
    while len(_cache) > 200:
        _cache.popitem(last=False)


class AssessIn(BaseModel):
    answers: dict[str, str]
    use_llm: bool = True


@app.middleware("http")
async def guards(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        cross = (origin and origin.split("://", 1)[-1] != request.headers.get("host")) \
            or (not origin and request.headers.get("sec-fetch-site") == "cross-site")
        if cross:
            return JSONResponse({"detail": "허용되지 않은 요청이에요."}, status_code=403)
        if "chunked" in request.headers.get("transfer-encoding", "").lower():  # 길이 제한을 우회하는 전송 방식은 받지 않는다
            return JSONResponse({"detail": "요청 길이를 알 수 없어요."}, status_code=411)
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY:
        return JSONResponse({"detail": "요청이 너무 큽니다."}, status_code=413)
    try:
        resp = await call_next(request)
    except Exception:  # 내부 오류 상세는 숨기고 서버 로그에만 남긴다
        logging.getLogger("palacewalk").exception("unhandled")
        resp = JSONResponse({"detail": "서버 오류가 발생했어요. 잠시 후 다시 시도해 주세요."}, status_code=500)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse({"detail": "요청 형식이 올바르지 않아요."}, status_code=422)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    headers = {"Retry-After": "30"} if exc.status_code == 429 else None
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.get("/api/questions")
def questions():
    return assess.public_questions()


def _parse(text: str):
    obj = llm.extract_json(text, accept=assess.parse_interp)
    return assess.parse_interp(obj) if obj else None


@app.post("/api/assess")
async def do_assess(body: AssessIn, request: Request):
    err = assess.validate_answers(body.answers) if len(body.answers) <= 20 else "알 수 없는 문항이 있습니다"
    if err:
        raise HTTPException(422, err)
    llm.check_rate_limit(client_ip(request), "assess")
    profile = assess.aggregate(body.answers)
    result = {"profile": profile, "summary": assess.rule_summary(profile), "source": "rules", "model": None,
              "notice": None, "ai_want": None}
    user = auth.user_from_request(request)

    def remember():  # 로그인한 사용자의 진단 이력(F3-5)
        if user:
            with db.conn() as c:
                saved.record_assessment(c, user["id"], body.answers, result["summary"])
    if not body.use_llm:
        remember()
        return result
    if not llm.API_KEY:
        result["notice"] = "해석 서비스가 설정되지 않아 기본 해석으로 보여드려요."
        remember()
        return result
    ckey = hashlib.sha256(json.dumps(sorted(body.answers.items())).encode()).hexdigest()
    cached = cache_get(ckey)
    if cached:
        result.update(cached)
        remember()
        return result
    out = await llm.run_chain(TEXT_MODELS, assess.build_messages(profile), _parse, max_tokens=1200, budget=40)
    if "value" in out:
        v = out["value"]
        # 규칙 기반 값이 정답: 프로필의 want는 유지하고 LLM의 want는 참고용(ai_want)으로만 둔다.
        update = {"summary": v["summary"], "source": "llm", "model": out["model"], "ai_want": v["want"]}
        cache_put(ckey, update)
        result.update(update)
    else:
        result["notice"] = ("해석 서비스 인증에 문제가 있어 기본 해석으로 보여드려요." if out.get("auth_error")
                            else "AI 해석이 지연되어 기본 해석으로 보여드려요.")
    remember()
    return result


@app.post("/api/recommend")
async def do_recommend(body: AssessIn, request: Request):
    """2단계: 답변에서 프로필을 서버가 다시 계산(클라이언트 프로필을 신뢰하지 않음)한 뒤 장소를 추천한다."""
    err = assess.validate_answers(body.answers) if len(body.answers) <= 20 else "알 수 없는 문항이 있습니다"
    if err:
        raise HTTPException(422, err)
    llm.check_rate_limit(client_ip(request), "recommend")
    places = recommend.load_places()
    if len(places) < 3:
        raise HTTPException(503, "장소 정보가 아직 준비되지 않았어요.")
    profile = assess.aggregate(body.answers)
    ranked = recommend.rank(places, profile)
    cands, by_id = recommend.candidates(ranked), {p["id"]: p for p in places}
    choice, source, model, notice = recommend.recommend_rules(ranked, profile), "rules", None, None
    if body.use_llm and llm.API_KEY:
        ckey = "rec:" + hashlib.sha256(json.dumps(sorted(body.answers.items())).encode()).hexdigest()
        cached = cache_get(ckey)
        if cached:
            choice, source, model = cached["choice"], "llm", cached["model"]
        else:
            ids = {c["place"]["id"] for c in cands}
            texts = {c["place"]["id"]: json.dumps(c["place"], ensure_ascii=False) for c in cands}
            parser = recommend.make_parser(ids, texts)
            out = await llm.run_chain(TEXT_MODELS, recommend.build_messages(profile, cands),
                                      lambda t: _parse_with(parser, t), max_tokens=3000, budget=55)
            if "value" in out:
                choice, source, model = out["value"], "llm", out["model"]
                cache_put(ckey, {"choice": choice, "model": model})
            else:
                notice = ("해석 서비스 인증에 문제가 있어 기본 추천으로 보여드려요." if out.get("auth_error")
                          else "AI 설명이 지연되어 기본 추천으로 보여드려요.")
    elif body.use_llm:
        notice = "해석 서비스가 설정되지 않아 기본 추천으로 보여드려요."
    cards = recommend.assemble(choice, by_id, ranked)
    return {"profile": profile, **cards, "source": source, "model": model, "notice": notice}


def _parse_with(parser, text):
    obj = llm.extract_json(text, accept=parser)
    return parser(obj) if obj else None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
