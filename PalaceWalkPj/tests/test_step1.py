import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import assess, llm, main

client = TestClient(main.app)
ALL_A = {q["id"]: "a" for q in assess.QUESTIONS["questions"]}
ALL_D = {q["id"]: "d" for q in assess.QUESTIONS["questions"]}


def fake_chain(result):
    async def f(*a, **k):
        return result
    return f


class Step1(unittest.TestCase):
    def setUp(self):
        llm._hits.clear()

    def test_20_questions_and_no_scoring_leak(self):
        d = client.get("/api/questions").json()
        self.assertEqual(len(d["questions"]), 20)
        counts = {}
        for q in d["questions"]:
            counts[q["section"]] = counts.get(q["section"], 0) + 1
            self.assertEqual(len(q["options"]), 4)
        self.assertEqual(counts, {"mind": 5, "body": 5, "taste": 10})
        raw = json.dumps(d)
        self.assertNotIn('"set"', raw)
        self.assertNotIn('"tags"', raw)

    def test_incomplete_rejected(self):
        a = dict(ALL_A); a.pop("t10")
        self.assertEqual(client.post("/api/assess", json={"answers": a, "use_llm": False}).status_code, 422)

    def test_bad_option_rejected(self):
        a = dict(ALL_A); a["m1"] = "z"
        self.assertEqual(client.post("/api/assess", json={"answers": a, "use_llm": False}).status_code, 422)

    def test_deterministic(self):
        r1 = client.post("/api/assess", json={"answers": ALL_A, "use_llm": False}).json()
        r2 = client.post("/api/assess", json={"answers": ALL_A, "use_llm": False}).json()
        self.assertEqual(r1, r2)
        self.assertEqual(r1["source"], "rules")

    def test_profiles_differ(self):
        a = client.post("/api/assess", json={"answers": ALL_A, "use_llm": False}).json()["profile"]
        d = client.post("/api/assess", json={"answers": ALL_D, "use_llm": False}).json()["profile"]
        self.assertEqual(a["body"]["max_steps"], 2000)
        self.assertFalse(a["body"]["stairs_ok"])
        self.assertEqual(d["body"]["max_steps"], 9000)
        self.assertTrue(d["body"]["stairs_ok"])

    def test_llm_success_only_overrides_interp_fields(self):
        ok = {"value": {"summary": "지친 하루, 조용한 곳에서 쉬어가요.", "want": "quiet"}, "model": "m1"}
        with mock.patch.object(llm, "API_KEY", "x"), mock.patch.object(llm, "run_chain", fake_chain(ok)):
            r = client.post("/api/assess", json={"answers": ALL_A}).json()
        self.assertEqual(r["source"], "llm")
        self.assertEqual(r["profile"]["body"]["max_steps"], 2000)  # 규칙 값 유지
        self.assertEqual(r["profile"]["mind"]["want"], "comfort")  # 규칙값 유지(ALL_A: m5=a)
        self.assertEqual(r["ai_want"], "quiet")

    def test_llm_failure_falls_back_to_rules(self):
        for out in ({"errors": ["m: transient"]}, {"auth_error": True}):
            with mock.patch.object(llm, "API_KEY", "x"), mock.patch.object(llm, "run_chain", fake_chain(out)):
                r = client.post("/api/assess", json={"answers": ALL_D}).json()
            self.assertEqual(r["source"], "rules")
            self.assertTrue(r["notice"])
            self.assertTrue(r["summary"])

    def test_parse_interp_rejects_bad(self):
        self.assertIsNone(assess.parse_interp({"summary": "충분히 긴 요약문입니다", "want": "bogus"}))
        self.assertIsNone(assess.parse_interp({"summary": "", "want": "quiet"}))
        self.assertIsNone(assess.parse_interp({"summary": "조용함을求하는 상태입니다", "want": "quiet"}))
        self.assertIsNotNone(assess.parse_interp({"summary": "충분히 긴 요약문입니다", "want": "quiet"}))

    def test_oversize_and_unknown_rejected(self):
        big = {f"x{i}": "a" for i in range(30)}
        self.assertEqual(client.post("/api/assess", json={"answers": big, "use_llm": False}).status_code, 422)
        self.assertEqual(client.post("/api/assess", content=b"{" + b" " * 9000 + b"}",
                                     headers={"content-type": "application/json"}).status_code, 413)

    def test_docs_hidden_and_headers(self):
        self.assertEqual(client.get("/docs").status_code, 404)
        r = client.get("/api/questions")
        self.assertIn("Content-Security-Policy", r.headers)

    def test_llm_weird_output_does_not_500(self):
        async def weird(client_, model, messages, parse, max_tokens, timeout):
            return None, "parse"
        for content in ('{"summary":"충분히 긴 요약문입니다","want":["quiet"]}', "no json", '{"summary": 5}'):
            self.assertIsNone(main._parse(content))

    def test_extract_json_skips_schema_example(self):
        txt = '형식 예: {"summary":"...","want":"x"} 정답: {"summary":"충분히 긴 요약문입니다","want":"quiet"}'
        self.assertEqual(llm.extract_json(txt, accept=assess.parse_interp)["want"], "quiet")

    def test_rate_limit(self):
        codes = [client.post("/api/assess", json={"answers": ALL_A, "use_llm": False}).status_code for _ in range(12)]
        self.assertEqual(codes.count(429), 2)

    def test_key_not_in_responses(self):
        if not llm.API_KEY:
            self.skipTest("no key")
        body = client.get("/static/app.js").text + client.get("/").text + client.get("/api/questions").text
        self.assertNotIn(llm.API_KEY, body)


if __name__ == "__main__":
    unittest.main()
