"""추천 장소 저장·산책 노트·진단 이력 (PRD_step3 F3-2~F3-6)."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from . import assess, db, recommend
from .auth import clean_text, current_user, now_iso, user_from_request

router = APIRouter()


class SaveIn(BaseModel):
    place_id: str = Field(..., max_length=80)
    why: str = Field("", max_length=600)
    answers: Optional[dict] = None
    summary: str = Field("", max_length=300)


class PatchIn(BaseModel):
    visited: Optional[bool] = None
    memo: Optional[str] = Field(None, max_length=500)


def record_assessment(c, user_id: int, answers: dict, summary: str = "") -> int:
    """진단 이력 저장. 프로필은 항상 서버가 답변에서 다시 계산한다. 직전과 같은 답이면 새로 만들지 않는다."""
    profile = assess.aggregate(answers)
    a_json = json.dumps(answers, sort_keys=True)
    last = c.execute("SELECT id, answers_json, summary FROM assessments WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                     (user_id,)).fetchone()
    if last and last["answers_json"] == a_json:
        if summary and not last["summary"]:
            c.execute("UPDATE assessments SET summary = ? WHERE id = ?", (summary, last["id"]))
        return last["id"]
    cur = c.execute("INSERT INTO assessments (user_id, answers_json, profile_json, summary, created_at) VALUES (?,?,?,?,?)",
                    (user_id, a_json, json.dumps(profile, ensure_ascii=False), clean_text(summary, 300), now_iso()))
    return cur.lastrowid


def place_view(row, places_by_id):
    p = places_by_id.get(row["place_id"])
    out = {"id": row["id"], "place_id": row["place_id"], "why": row["why_snapshot"], "visited": bool(row["visited"]),
           "visited_at": row["visited_at"], "memo": row["memo"], "saved_at": row["created_at"], "place": None, "outdated": p is None}
    if p:
        out["place"] = recommend.public_place(p)
    return out


def own_row(c, user_id: int, saved_id: int):
    row = c.execute("SELECT * FROM saved_places WHERE id = ? AND user_id = ?", (saved_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "저장한 장소를 찾을 수 없어요.")  # 남의 항목도 같은 응답(존재 여부 노출 방지)
    return row


@router.post("/api/saved")
def save_place(body: SaveIn, user=Depends(current_user)):
    places = {p["id"]: p for p in recommend.load_places()}
    if body.place_id not in places:
        raise HTTPException(422, "알 수 없는 장소예요.")
    answers = body.answers if body.answers and not assess.validate_answers(body.answers) else None
    with db.conn() as c:
        existing = c.execute("SELECT * FROM saved_places WHERE user_id = ? AND place_id = ?", (user["id"], body.place_id)).fetchone()
        if existing:  # 중복 저장 방지: 기존 항목을 그대로 돌려주고, 이력은 건드리지 않는다
            return {"item": place_view(existing, places), "created": False}
        aid = record_assessment(c, user["id"], answers, body.summary) if answers else None
        cur = c.execute("INSERT INTO saved_places (user_id, place_id, assessment_id, why_snapshot, created_at) VALUES (?,?,?,?,?)",
                        (user["id"], body.place_id, aid, clean_text(body.why, 600), now_iso()))
        row = c.execute("SELECT * FROM saved_places WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"item": place_view(row, places), "created": True}


@router.get("/api/saved")
def list_saved(palace: Optional[str] = None, user=Depends(current_user)):
    places = {p["id"]: p for p in recommend.load_places()}
    with db.conn() as c:
        rows = c.execute("SELECT * FROM saved_places WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
    items = [place_view(r, places) for r in rows]
    if palace:
        items = [i for i in items if i["place"] and i["place"]["palace"] == palace]
    return {"items": items, "palaces": sorted({p["palace"] for p in places.values()})}


@router.patch("/api/saved/{saved_id}")
def patch_saved(body: PatchIn, saved_id: int = Path(..., ge=1, le=2**62), user=Depends(current_user)):
    with db.conn() as c:
        row = own_row(c, user["id"], saved_id)
        visited = row["visited"] if body.visited is None else int(body.visited)
        visited_at = row["visited_at"]
        if body.visited is True and not row["visited"]:
            visited_at = now_iso()
        elif body.visited is False:
            visited_at = None
        memo = row["memo"] if body.memo is None else clean_text(body.memo, 500)
        c.execute("UPDATE saved_places SET visited = ?, visited_at = ?, memo = ? WHERE id = ?", (visited, visited_at, memo, saved_id))
        row = c.execute("SELECT * FROM saved_places WHERE id = ?", (saved_id,)).fetchone()
    return {"item": place_view(row, {p["id"]: p for p in recommend.load_places()})}


@router.delete("/api/saved/{saved_id}")
def delete_saved(saved_id: int = Path(..., ge=1, le=2**62), user=Depends(current_user)):
    with db.conn() as c:
        own_row(c, user["id"], saved_id)
        c.execute("DELETE FROM saved_places WHERE id = ?", (saved_id,))
    return {"ok": True}


@router.get("/api/assessments")
def list_assessments(user=Depends(current_user)):
    with db.conn() as c:
        rows = c.execute("SELECT id, profile_json, summary, created_at FROM assessments WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                         (user["id"],)).fetchall()
    return {"items": [{"id": r["id"], "summary": r["summary"], "created_at": r["created_at"],
                       "profile": json.loads(r["profile_json"])} for r in rows]}


@router.get("/api/assessments/last-answers")
def last_answers(user=Depends(current_user)):
    """지난번 취향 불러오기(F3-6): 마지막 진단의 취향 문항(t1~t10) 답만 돌려준다."""
    with db.conn() as c:
        row = c.execute("SELECT answers_json FROM assessments WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if not row:
        return {"answers": None}
    a = json.loads(row["answers_json"])
    return {"answers": {k: v for k, v in a.items() if k.startswith("t")}}
