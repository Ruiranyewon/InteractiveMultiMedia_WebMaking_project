import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import assess, llm, main, recommend

client = TestClient(main.app)


def P(id, mood, aest, hist, steps, stairs, rest, dur):
    return {"id": id, "palace": "테스트궁", "name": id, "facts": "사실 문장입니다.", "feel": "분위기 문장이에요.",
            "tags": {"mood": mood, "aesthetic": aest, "history": hist},
            "walk": {"steps_from_entrance": steps, "stairs": stairs, "rest_spots": rest, "duration_min": dur},
            "image": None, "sources": ["https://example.org"], "verified_on": "2026-10-09"}


PLACES = [
    P("quiet-garden", ["quiet", "hidden"], ["garden", "water"], ["people"], 800, "none", True, 40),
    P("grand-hall", ["grand", "open"], ["structure", "view"], ["politics"], 900, "few", False, 30),
    P("far-hill", ["quiet"], ["garden"], ["people"], 4000, "many", False, 120),
    P("color-gate", ["open"], ["color"], ["architecture"], 1200, "few", True, 40),
    P("stone-yard", ["quiet"], ["stone", "eclectic"], ["architecture"], 1000, "none", True, 45),
    P("mid", ["hidden"], ["garden"], ["architecture"], 1500, "some", True, 60),
]
EXPLORER = {q["id"]: "d" for q in assess.QUESTIONS["questions"]}   # 활기·장거리
TIRED = {q["id"]: "a" for q in assess.QUESTIONS["questions"]}      # 피곤·평지·2000보
TIRED.update({"t3": "b", "t6": "a"})                                # 정원 + 고요 취향


def prof(a):
    return assess.aggregate(a)


class Step2(unittest.TestCase):
    def setUp(self):
        llm._hits.clear(); main._cache.clear()

    def test_tired_profile_never_gets_hard_place_first(self):
        r = recommend.rank(PLACES, prof(TIRED))
        self.assertNotIn(r[0]["place"]["id"], ("far-hill", "grand-hall"))
        self.assertGreater(r[0]["score"], -50)  # 크게 어긋난 장소가 아님
        self.assertEqual(r[0]["place"]["id"], "quiet-garden")

    def test_quiet_vs_grand_preference_differ(self):
        grand = dict(EXPLORER); grand.update({"t3": "a", "t6": "b", "t1": "a", "m5": "c"})
        quiet = dict(EXPLORER); quiet.update({"t3": "b", "t6": "a", "t1": "c", "m5": "d"})
        self.assertNotEqual(recommend.rank(PLACES, prof(grand))[0]["place"]["id"],
                            recommend.rank(PLACES, prof(quiet))[0]["place"]["id"])

    def test_rank_deterministic(self):
        a = [r["place"]["id"] for r in recommend.rank(PLACES, prof(TIRED))]
        b = [r["place"]["id"] for r in recommend.rank(list(reversed(PLACES)), prof(TIRED))]
        self.assertEqual(a, b)

    def test_all_unfit_still_returns_lowest_burden(self):
        hard = [P("h1", ["quiet"], ["garden"], ["people"], 5000, "many", False, 150),
                P("h2", ["quiet"], ["garden"], ["people"], 3000, "many", False, 150)] * 1
        r = recommend.rank(hard, prof(TIRED))
        self.assertEqual(r[0]["place"]["id"], "h2")

    def test_parser_rejects_unknown_id_and_foreign(self):
        ids = {"quiet-garden", "mid", "stone-yard"}
        parse = recommend.make_parser(ids)
        good = {"primary": {"place_id": "mid", "why": "오늘은 조용한 곳이 좋아요. 이곳이 어울려요.", "highlights": ["연못 풍경", "나무 그늘", "고요한 길"], "tip": "천천히 걸어 보세요."},
                "alternatives": [{"place_id": "stone-yard", "one_line": "석조가 인상적이에요."}, {"place_id": "nope", "one_line": "없는 곳이에요."}]}
        out = parse(good)
        self.assertEqual(out["primary"]["place_id"], "mid")
        self.assertEqual([a["place_id"] for a in out["alts"]], ["stone-yard"])
        bad = {**good, "primary": {**good["primary"], "place_id": "invented"}}
        self.assertIsNone(parse(bad))
        cjk = {**good, "primary": {**good["primary"], "why": "조용한 곳이 좋은 날이라 靜 이곳이 어울려요."}}
        self.assertIsNone(parse(cjk))
        weird = {"primary": {"place_id": ["mid"]}}
        self.assertIsNone(parse(weird))

    def _post(self, answers, llm_out=None, use_llm=True):
        async def fake(*a, **k):
            return llm_out
        with mock.patch.object(recommend, "load_places", return_value=PLACES), \
             mock.patch.object(llm, "API_KEY", "x"), mock.patch.object(llm, "run_chain", fake):
            return client.post("/api/recommend", json={"answers": answers, "use_llm": use_llm})

    def test_endpoint_rules_when_llm_fails(self):
        for out in ({"errors": ["x"]}, {"auth_error": True}):
            r = self._post(TIRED, out).json()
            self.assertEqual(r["source"], "rules")
            self.assertTrue(r["notice"])
            self.assertEqual(r["primary"]["id"], "quiet-garden")
            self.assertEqual(len(r["alternatives"]), 2)
            self.assertTrue(r["primary"]["facts"])

    def test_endpoint_llm_choice_used_and_facts_from_db(self):
        out = {"value": {"primary": {"place_id": "stone-yard", "why": "조용히 쉬기에 좋은 곳이에요. 평지라 무리가 없어요.",
                                      "highlights": ["석조 질감", "고즈넉한 마당"], "tip": "천천히 걸어요."},
                         "alts": [{"place_id": "quiet-garden", "one_line": "연못이 있어요."}]}, "model": "m1"}
        r = self._post(TIRED, out).json()
        self.assertEqual(r["source"], "llm")
        self.assertEqual(r["primary"]["id"], "stone-yard")
        self.assertEqual(r["primary"]["facts"], "사실 문장입니다.")  # LLM이 아닌 DB 값
        self.assertEqual(len(r["alternatives"]), 2)  # 모자란 대안은 규칙 순위로 보충
        self.assertEqual(len({r["primary"]["id"], *[a["id"] for a in r["alternatives"]]}), 3)

    def test_endpoint_validation_and_no_places(self):
        a = dict(TIRED); a.pop("t1")
        self.assertEqual(self._post(a).status_code, 422)
        with mock.patch.object(recommend, "load_places", return_value=[]):
            self.assertEqual(client.post("/api/recommend", json={"answers": TIRED}).status_code, 503)

    def test_public_place_hides_internal_fields(self):
        self.assertNotIn("tags", recommend.public_place(PLACES[0]))

    def test_hard_limits_hold_on_real_db(self):
        import random
        places = recommend.load_places()
        self.assertGreaterEqual(len(places), 15)
        rnd = random.Random(3)
        qs = assess.QUESTIONS["questions"]
        for _ in range(1500):
            a = {q["id"]: rnd.choice("abcd") for q in qs}
            r = recommend.rank(places, assess.aggregate(a))
            if any(x["fits"] for x in r):
                self.assertTrue(r[0]["fits"])
            c = recommend.candidates(r)
            if sum(x["fits"] for x in r) >= 3:
                self.assertTrue(all(x["fits"] for x in c))

    def test_real_db_integrity_and_access_note(self):
        allowed = {"mood": {"quiet", "grand", "hidden", "open", "water", "shade"},
                   "aesthetic": {"structure", "garden", "color", "stone", "eclectic", "view", "water", "shade"},
                   "history": {"politics", "architecture", "people"}}
        places = recommend.load_places()
        self.assertEqual(len({p["id"] for p in places}), len(places))
        for p in places:
            for cat, vals in p["tags"].items():
                self.assertTrue(set(vals) <= allowed[cat], (p["id"], cat, vals))
            self.assertTrue(recommend.public_place(p)["access_note"])  # 유의사항은 항상 표시

    def test_invented_facility_rejected(self):
        parse = recommend.make_parser({"mid", "stone-yard"}, {"mid": "연못 정원", "stone-yard": "마당 벤치"})
        base = {"primary": {"place_id": "mid", "why": "조용히 쉬기 좋은 곳이에요. 연못이 있어요.", "highlights": ["연못 풍경"], "tip": "천천히 걸어요."}, "alternatives": []}
        self.assertIsNotNone(parse(base))
        bad = {**base, "primary": {**base["primary"], "tip": "벤치에 앉아 쉬어요."}}
        self.assertIsNone(parse(bad))  # mid의 DB 문장에는 벤치가 없다
        ok = {**base, "alternatives": [{"place_id": "stone-yard", "one_line": "벤치에서 쉬기 좋아요."}]}
        self.assertEqual(len(parse(ok)["alts"]), 1)  # stone-yard에는 벤치가 있다

    def test_candidates_only_fitting_when_any(self):
        r = recommend.rank(PLACES, prof(TIRED))
        c = recommend.candidates(r)
        if any(x["fits"] for x in r):
            self.assertTrue(all(x["fits"] for x in c))

    def test_english_words_rejected(self):
        self.assertIsNone(assess.parse_interp({"summary": "쉼터에서 잠시 descans 할 수 있어요", "want": "quiet"}))

    def test_broken_places_file_is_empty_not_crash(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "p.json"
            f.write_text("{not json")
            self.assertEqual(recommend.load_places(f), [])
            f.write_text("[1,2]")
            self.assertEqual(recommend.load_places(f), [])


if __name__ == "__main__":
    unittest.main()
