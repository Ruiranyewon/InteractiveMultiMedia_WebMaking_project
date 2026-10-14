"""1단계: 응답 집계(규칙 기반 = 정답) + LLM 해석문 생성."""
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))
QMAP = {q["id"]: q for q in QUESTIONS["questions"]}
WANTS = {"comfort", "refresh", "inspire", "quiet"}
WANT_KO = {"comfort": "위로와 안정", "refresh": "기분 전환", "inspire": "영감", "quiet": "고요"}
WANT_WISH = {"comfort": "위로와 안정을 찾고", "refresh": "기분 전환을 원하고", "inspire": "새로운 영감을 얻고 싶고", "quiet": "고요를 원하고"}


def public_questions():
    """브라우저에는 점수 매핑(set/tags)을 보내지 않는다."""
    qs = [{"id": q["id"], "section": q["section"], "category": q.get("category"), "text": q["text"],
           "options": [{"id": o["id"], "text": o["text"]} for o in q["options"]]} for q in QUESTIONS["questions"]]
    return {"sections": QUESTIONS["sections"], "questions": qs}


def validate_answers(answers: dict):
    """모든 문항에 유효한 선택지 ID가 있어야 한다. 오류 메시지 또는 None."""
    if set(answers) != set(QMAP):
        missing = sorted(set(QMAP) - set(answers))
        return f"모든 문항에 답해 주세요 (미응답 {len(missing)}개)" if missing else "알 수 없는 문항이 있습니다"
    for qid, oid in answers.items():
        if oid not in {o["id"] for o in QMAP[qid]["options"]}:
            return f"문항 {qid}의 선택지가 올바르지 않습니다"
    return None


def aggregate(answers: dict) -> dict:
    """같은 응답이면 항상 같은 결과(결정적)."""
    vals, tags = {}, {"history": Counter(), "aesthetic": Counter(), "mood": Counter()}
    for qid, oid in answers.items():
        opt = next(o for o in QMAP[qid]["options"] if o["id"] == oid)
        vals.update(opt.get("set", {}))
        for cat, lst in opt.get("tags", {}).items():
            tags[cat].update(lst)

    def top(c, n=3):
        return [t for t, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])) if t != "none"][:n]

    return {
        "mind": {"energy": vals["energy"], "tension": vals["tension"], "clutter": vals["clutter"],
                 "company_mood": vals["company_mood"], "want": vals["want"]},
        "body": {"fatigue": vals["fatigue"], "max_steps": vals["max_steps"], "stairs": vals["stairs"],
                 "stairs_ok": vals["stairs"] in ("some", "many"), "outdoor_min": vals["outdoor_min"],
                 "needs_rest": vals["rest"] >= 2},
        "taste": {"history": top(tags["history"]), "aesthetic": top(tags["aesthetic"]), "mood": top(tags["mood"]),
                  "etc": {"companion": vals["companion"], "photo": vals["photo"], "depth": vals["depth"]}},
    }


def rule_summary(p: dict) -> str:
    m, b, t = p["mind"], p["body"], p["taste"]
    mood = "지치고 긴장된" if m["tension"] >= 2 and m["energy"] <= 1 else "마음이 무거운" if m["energy"] <= 1 \
        else "긴장이 쌓인" if m["tension"] >= 2 else "비교적 가벼운"
    body = "몸도 쉬엄쉬엄 움직여야 하는" if b["fatigue"] >= 2 else "몸은 움직일 여력이 있는"
    return f"오늘은 {mood} 상태로, {WANT_WISH[m['want']]} {body} 날이에요. 약 {b['max_steps']:,}보 안쪽의 산책이 알맞아요."


COMP_KO = {"alone": "혼자", "couple": "연인·배우자", "friends": "친구·가족", "care": "보호가 필요한 동행(아이·부모님 등)"}
TAG_KO = {"politics": "정치·권력 이야기", "architecture": "건축·의례", "people": "사람들의 삶", "structure": "건물의 구조미",
          "garden": "정원·자연", "color": "단청·색채", "stone": "석조·담장의 질감", "eclectic": "동서양이 섞인 풍경", "view": "전망",
          "water": "물", "shade": "그늘", "quiet": "고요함", "grand": "웅장함", "hidden": "아늑하고 은밀함", "open": "탁 트임"}


def _ko(tags, table):
    return ", ".join(table.get(t, t) for t in tags)


LEVEL = ["전혀 없음", "약간", "꽤 많음", "매우 많음"]
STAIRS_KO = {"none": "계단·오르막은 피하고 싶음", "few": "계단은 아주 조금만 가능", "some": "보통 정도의 계단은 가능", "many": "언덕도 문제없음"}


def describe(p: dict) -> str:
    """LLM이 숫자 척도를 오해하지 않도록 사람이 읽는 문장으로 풀어 준다."""
    m, b, t = p["mind"], p["body"], p["taste"]
    return (f"- 마음 에너지: {['바닥남', '낮음', '보통', '높음'][m['energy']]}\n"
            f"- 긴장·스트레스: {LEVEL[m['tension']]}\n"
            f"- 머릿속 복잡함: {LEVEL[m['clutter']]}\n"
            f"- 몸의 피로: {LEVEL[b['fatigue']]}\n"
            f"- 걸을 수 있는 거리: 약 {b['max_steps']}보, {STAIRS_KO[b['stairs']]}\n"
            f"- 쉼터 필요: {'예' if b['needs_rest'] else '아니오'}\n"
            f"- 산책에서 원하는 것: {WANT_KO[m['want']]}\n"
            f"- 함께하는 사람: {COMP_KO[t['etc']['companion']]}\n"
            f"- 끌리는 이야기: {_ko(t['history'], TAG_KO) or '특별히 없음'}\n"
            f"- 끌리는 아름다움: {_ko(t['aesthetic'], TAG_KO) or '특별히 없음'}\n"
            f"- 끌리는 분위기: {_ko(t['mood'], TAG_KO) or '특별히 없음'}\n"
            f"- 사진 촬영 비중: {['눈으로만', '거의 안 찍음', '예쁘면 찍음', '매우 중요'][t['etc']['photo']]}\n"
            f"- 듣고 싶은 설명: {['한두 줄', '핵심만', '배경까지', '자세히'][t['etc']['depth']]}")


def build_messages(p: dict):
    sys = ("너는 궁궐 산책 추천 서비스의 상태 해석 도우미다. 아래 사용자 상태를 보고 오늘의 상태를 따뜻하고 과장 없이 "
           "한국어 1~2문장으로, '~이에요/~해요' 체로 사용자에게 직접 말하듯 요약한다. 주어진 상태와 모순되는 말을 하지 말고, 한글 이외의 문자(한자·영어 등)는 쓰지 않는다. "
           "산책에서 얻고 싶은 것(want)은 comfort|refresh|inspire|quiet 중 하나로 고른다. "
           "설명 없이 JSON 객체 하나만 출력한다. 형식: {\"summary\":\"...\",\"want\":\"quiet\"}")
    return [{"role": "system", "content": sys}, {"role": "user", "content": describe(p)}]


FOREIGN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]|[A-Za-z]{2,}")  # 한자·가나·영어 단어가 섞인 출력은 거부


def parse_interp(obj: dict):
    """LLM 출력 검증. 형식이 틀리면 None(→ 다른 모델/규칙 기반)."""
    s, w = obj.get("summary"), obj.get("want")
    if not isinstance(s, str) or not (5 <= len(s.strip()) <= 300) or not isinstance(w, str) or w not in WANTS \
            or FOREIGN.search(s):
        return None
    return {"summary": s.strip(), "want": w}
