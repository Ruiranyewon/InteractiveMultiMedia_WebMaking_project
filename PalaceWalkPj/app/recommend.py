"""2단계: 후보 DB 필터·점수화(규칙) + LLM 최종 선택·추천 이유 + 사실 정보는 DB에서만."""
import hashlib
import json
import re
from pathlib import Path

from . import assess

ROOT = Path(__file__).resolve().parent.parent
PLACES_PATH = ROOT / "data" / "places.json"
STAIRS_RANK = {"none": 0, "few": 1, "some": 2, "many": 3}
FOREIGN = assess.FOREIGN
REQUIRED = {"id", "palace", "name", "tags", "walk", "facts", "feel"}
PUBLIC_FIELDS = ("id", "palace", "name", "facts", "feel", "walk", "image", "access_note", "sources", "verified_on")


def load_places(path: Path = PLACES_PATH):
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        places = doc.get("places", []) if isinstance(doc, dict) else []
        return [p for p in places if isinstance(p, dict) and REQUIRED <= set(p)]
    except (OSError, ValueError):
        return []


def burden(place: dict) -> float:
    """왕복 걸음 수 + 머무는 시간으로 본 신체 부담(작을수록 쉬움)."""
    w = place["walk"]
    return w["steps_from_entrance"] * 2 + w["duration_min"] * 40


def score(place: dict, profile: dict):
    """(점수, 설명용 일치 태그 리스트, 신체 적합 여부, 한도 초과 정도). 같은 입력이면 항상 같은 값."""
    m, b, t = profile["mind"], profile["body"], profile["taste"]
    tags, hits, s = place["tags"], [], 0.0
    for cat, w in (("mood", 2.0), ("aesthetic", 2.0), ("history", 1.5)):
        for rank, tag in enumerate(t[cat]):
            if tag in tags.get(cat, []):
                s += w * (1 - 0.15 * rank)
                hits.append(tag)
    # 마음 상태 → 분위기 보정
    calm = {"quiet", "hidden", "water", "shade"}
    pt = set(tags.get("mood", [])) | set(tags.get("aesthetic", []))
    if m["want"] in ("quiet", "comfort") or m["tension"] >= 2:
        s += 1.5 * len(pt & calm) / 2
        if "grand" in tags.get("mood", []) and m["tension"] >= 2:
            s -= 1.0
    if m["want"] == "refresh":
        s += 1.5 * len(pt & {"open", "view", "water"}) / 2
    if m["want"] == "inspire":
        s += 1.5 * len(set(tags.get("history", [])) & {"architecture", "politics", "people"}) / 2 + (0.5 if "eclectic" in pt else 0)
    if t["etc"]["photo"] >= 2 and pt & {"view", "color", "eclectic", "grand"}:
        s += 1.0
    # 신체 적합도. hard: 계단·걸음 한도 위반(다른 후보가 있는 한 1순위가 될 수 없다), soft: 시간·쉼터 감점.
    w = place["walk"]
    over_stairs = STAIRS_RANK[w["stairs"]] - STAIRS_RANK[b["stairs"]]
    over_steps = w["steps_from_entrance"] * 2 / max(b["max_steps"], 1)
    over_time = w["duration_min"] / max(b["outdoor_min"], 1)
    hard = over_stairs > 0 or over_steps > 1.15
    pen = 0.0
    if over_stairs > 0:
        pen += 3.0 * over_stairs
    if over_steps > 1.15:
        pen += 5.0 * (over_steps - 1.15) + 1.0
    if over_time > 1.25:
        pen += 3.0 * (over_time - 1.25) + 0.5
    if b["needs_rest"] and not w["rest_spots"]:
        pen += 2.0
    # 태그가 거의 같은 장소끼리 늘 같은 곳만 이기지 않도록, 프로필에 따라 달라지는 아주 작은 결정적 가중치
    sig = hashlib.sha256((place["id"] + json.dumps(profile, sort_keys=True)).encode()).digest()[0] / 255 * 0.6
    viol = max(over_stairs, 0) + max(over_steps - 1.15, 0) * 2  # 한도를 얼마나 넘는지(한도 안이면 0)
    return s - pen + sig, list(dict.fromkeys(hits)), not hard, viol


def rank(places: list, profile: dict):
    rows = []
    for p in places:
        sc, hits, ok, viol = score(p, profile)
        rows.append({"place": p, "score": sc, "hits": hits, "fits": ok, "viol": viol})
    # 한도 안의 장소가 항상 먼저(점수순). 한도 안의 장소가 없을 때만 부담이 작은 순으로 보충한다.
    ok = sorted([r for r in rows if r["fits"]], key=lambda r: (-r["score"], r["place"]["id"]))
    over = sorted([r for r in rows if not r["fits"]], key=lambda r: (round(r["viol"], 1), -r["score"], r["place"]["id"]))
    return ok + over


def candidates(ranked: list, n: int = 4):
    """LLM에 줄 후보. 신체 한도 안의 장소가 있으면 한도를 넘는 장소는 후보에서 뺀다."""
    fit = [r for r in ranked if r["fits"]]
    return (fit or ranked)[:n]  # 한도 안의 장소가 하나라도 있으면 그 안에서만 고르게 한다


DEFAULT_ACCESS = "관람 시간·휴관일·예약 여부는 방문 전 공식 안내(국가유산청 궁능유적본부)에서 꼭 확인하세요."
HUWON_ACCESS = "후원은 일반 관람과 별도의 입장 방식이 있을 수 있어요. 예약·해설 관람 여부를 공식 안내에서 확인하세요."


def public_place(p: dict) -> dict:
    out = {k: p[k] for k in PUBLIC_FIELDS if k in p}
    out.setdefault("access_note", HUWON_ACCESS if "huwon" in p["id"] else DEFAULT_ACCESS)
    return out


def build_messages(profile: dict, cands: list):
    lines = []
    for i, c in enumerate(cands, 1):
        p = c["place"]
        t = p["tags"]
        lines.append(f"{i}. id={p['id']} | {p['palace']} {p['name']} | 분위기: {p['feel']} | 사실: {p['facts']} | "
                     f"성격: {assess._ko(t.get('mood', []) + t.get('aesthetic', []) + t.get('history', []), assess.TAG_KO)} | "
                     f"왕복 약 {p['walk']['steps_from_entrance']*2:,}보, 약 {p['walk']['duration_min']}분, 계단 {p['walk']['stairs']}, "
                     f"쉼터 {'있음' if p['walk']['rest_spots'] else '적음'}")
    sys = ("너는 궁궐 산책 추천 도우미다. 방문자의 상태·취향과 후보 목록을 보고 가장 어울리는 장소 1곳(primary)과 대안 2곳을 고른다.\n"
           "규칙:\n"
           "- 반드시 후보 목록의 id만 쓴다. 신체 조건(걸음·계단·쉼터)에 맞는 곳을 우선하고, 취향과 동행을 반영한다.\n"
           "- why·highlights·tip·one_line에는 후보의 '분위기'와 '사실'과 '성격'에 나온 소재만 쓴다. 거기에 없는 건물·시설·벤치·돌담·"
           "날씨·시간대·빛·소리·계절 묘사와 연도·수치는 절대 지어내지 않는다. 현재 모습이 확실하지 않으면 묘사하지 않는다.\n"
           "- 한글 이외의 문자(한자·영어·다른 나라 말)를 쓰지 않는다. '사용자'라는 말을 쓰지 말고 방문자에게 직접 말하듯 쓰고, 문장마다 '오늘'을 붙이지 않는다.\n"
           "- 문체는 '~이에요/~해요'체. '~합니다/~습니다' 체는 쓰지 않는다. 따뜻하되 과장하지 않는다.\n"
           "- why: 방문자의 상태와 장소를 연결한 2문장. highlights: 감상 포인트 3개(각 한 구절). tip: 한 문장. one_line: 한 문장.\n"
           "설명 없이 JSON 객체 하나만 출력한다. 형식: "
           '{"primary":{"place_id":"id","why":"...","highlights":["...","...","..."],"tip":"..."},'
           '"alternatives":[{"place_id":"id","one_line":"..."},{"place_id":"id","one_line":"..."}]}')
    user = "[방문자 상태와 취향]\n" + assess.describe(profile) + "\n\n[후보]\n" + "\n".join(lines)
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def _clean(s, lo, hi):
    return isinstance(s, str) and lo <= len(s.strip()) <= hi and not FOREIGN.search(s)


INVENTED = ("벤치", "의자", "카페", "매점", "돌담", "햇살", "햇빛", "노을", "새소리", "물소리", "바람", "단풍", "벚꽃", "눈이", "안개")


def make_parser(valid_ids: set, texts: dict = None):
    """texts: place_id -> 그 장소의 DB 문장 전체. 생성문에 DB에 없는 시설·감각 단어가 있으면 거부한다."""
    texts = texts or {}

    def invented(pid, *strs):
        base = texts.get(pid, "")
        return any(w in s and w not in base for s in strs for w in INVENTED)

    def parse(obj: dict):
        pr, alts = obj.get("primary"), obj.get("alternatives")
        if not isinstance(pr, dict) or not isinstance(pr.get("place_id"), str) or pr["place_id"] not in valid_ids:
            return None
        hl = pr.get("highlights")
        if isinstance(hl, list) and invented(pr["place_id"], str(pr.get("why")), str(pr.get("tip")), *map(str, hl)):
            return None
        if not (_clean(pr.get("why"), 10, 400) and _clean(pr.get("tip"), 3, 200)
                and isinstance(hl, list) and 1 <= len(hl) <= 5 and all(_clean(h, 2, 80) for h in hl)):
            return None
        out_alts, seen = [], {pr["place_id"]}
        for a in alts if isinstance(alts, list) else []:
            if isinstance(a, dict) and isinstance(a.get("place_id"), str) and a["place_id"] in valid_ids \
                    and a["place_id"] not in seen and _clean(a.get("one_line"), 3, 200) \
                    and not invented(a["place_id"], a["one_line"]):
                out_alts.append({"place_id": a["place_id"], "one_line": a["one_line"].strip()})
                seen.add(a["place_id"])
        return {"primary": {"place_id": pr["place_id"], "why": pr["why"].strip(),
                            "highlights": [h.strip() for h in hl], "tip": pr["tip"].strip()}, "alts": out_alts[:2]}
    return parse


TAG_KO = {"quiet": "고요한 분위기", "hidden": "아늑하고 은밀한 분위기", "grand": "웅장한 분위기", "open": "탁 트인 분위기",
          "water": "물의 풍경", "shade": "그늘", "structure": "건축의 구조미", "garden": "정원과 자연", "color": "단청과 색채",
          "stone": "석조와 담장의 질감", "eclectic": "동서양이 섞인 풍경", "view": "전망",
          "politics": "정치와 권력의 이야기", "architecture": "건축과 의례", "people": "사람들의 삶"}




def josa_eul(word: str) -> str:
    """받침 유무에 따라 을/를."""
    ch = word.strip()[-1:]
    if "가" <= ch <= "힣":
        return "을" if (ord(ch) - 0xAC00) % 28 else "를"
    return "을(를)"


def fallback_card(cand: dict, profile: dict) -> dict:
    p, hits = cand["place"], cand["hits"]
    names = [TAG_KO[h] for h in hits[:3] if h in TAG_KO]
    summ = assess.rule_summary(profile).split(".")[0] + "."
    why = summ + (f" {', '.join(names)}{josa_eul(names[-1])} 좋아하는 취향과 잘 맞는 곳이에요." if names else " 오늘의 걸음에 무리 없는 곳이에요.")
    return {"place_id": p["id"], "why": why, "highlights": [p["feel"]], "tip": "방문 전 공식 안내에서 관람 시간과 휴관일을 확인하세요."}


def recommend_rules(ranked: list, profile: dict) -> dict:
    top = ranked[0]
    return {"primary": fallback_card(top, profile),
            "alts": [{"place_id": r["place"]["id"], "one_line": r["place"]["feel"]} for r in ranked[1:3]]}


def assemble(choice: dict, by_id: dict, ranked: list) -> dict:
    """LLM이 고른 id를 DB와 대조해 카드로 조립. 대안이 모자라면 규칙 순위로 채운다."""
    prim_id = choice["primary"]["place_id"]
    alts = [a for a in choice["alts"] if a["place_id"] in by_id]
    used = {prim_id} | {a["place_id"] for a in alts}
    for r in ranked:
        if len(alts) >= 2:
            break
        if r["place"]["id"] not in used:
            alts.append({"place_id": r["place"]["id"], "one_line": r["place"]["feel"]})
            used.add(r["place"]["id"])
    return {
        "primary": {**public_place(by_id[prim_id]), "why": choice["primary"]["why"],
                    "highlights": choice["primary"]["highlights"], "tip": choice["primary"]["tip"]},
        "alternatives": [{**public_place(by_id[a["place_id"]]), "one_line": a["one_line"]} for a in alts],
    }
