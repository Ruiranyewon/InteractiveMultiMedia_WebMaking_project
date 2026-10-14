import os
import stat
import unittest

from fastapi.testclient import TestClient

from app import assess, db, llm, main

ANS = {q["id"]: "b" for q in assess.QUESTIONS["questions"]}
ANS2 = {q["id"]: "c" for q in assess.QUESTIONS["questions"]}
PID = "deoksugung-seokjojeon"


def fresh():
    llm._hits.clear()
    return TestClient(main.app)


def signup(c, email="a@example.com", pw="password123", nick="가나"):
    return c.post("/api/auth/signup", json={"email": email, "password": pw, "nickname": nick})


class Step3(unittest.TestCase):
    def setUp(self):
        with db.conn() as c:
            for t in ("saved_places", "assessments", "sessions", "users"):
                c.execute(f"DELETE FROM {t}")
        self.c = fresh()

    def test_signup_login_logout_cycle(self):
        r = signup(self.c)
        self.assertEqual(r.status_code, 201)
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        self.assertIn("samesite=lax", r.headers["set-cookie"].lower())
        self.assertEqual(self.c.get("/api/me").json()["user"]["email"], "a@example.com")
        self.c.post("/api/auth/logout")
        self.assertIsNone(self.c.get("/api/me").json()["user"])
        c2 = fresh()
        self.assertEqual(c2.post("/api/auth/login", json={"email": "A@Example.com", "password": "password123"}).status_code, 200)
        self.assertEqual(c2.get("/api/me").json()["user"]["nickname"], "가나")

    def test_bad_credentials_generic(self):
        signup(self.c)
        c2 = fresh()
        a = c2.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
        b = c2.post("/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"})
        self.assertEqual((a.status_code, b.status_code), (401, 401))
        self.assertEqual(a.json(), b.json())

    def test_signup_validation_and_duplicate(self):
        self.assertEqual(signup(self.c, pw="short").status_code, 422)
        self.assertEqual(signup(self.c, email="not-an-email").status_code, 422)
        self.assertEqual(signup(self.c).status_code, 201)
        self.assertEqual(signup(fresh()).status_code, 409)

    def test_password_not_plaintext(self):
        signup(self.c, pw="password123")
        with db.conn() as c:
            h = c.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertTrue(h.startswith("$argon2"))
        self.assertNotIn("password123", h)

    def test_login_rate_limit(self):
        signup(self.c)
        c2 = fresh()
        codes = [c2.post("/api/auth/login", json={"email": "a@example.com", "password": "bad-password"}).status_code for _ in range(7)]
        self.assertEqual(codes.count(429), 2)

    def test_save_requires_login_and_validates(self):
        self.assertEqual(self.c.post("/api/saved", json={"place_id": PID}).status_code, 401)
        self.assertEqual(self.c.get("/api/saved").status_code, 401)
        signup(self.c)
        self.assertEqual(self.c.post("/api/saved", json={"place_id": "nope"}).status_code, 422)

    def test_save_is_idempotent_and_listed(self):
        signup(self.c)
        r1 = self.c.post("/api/saved", json={"place_id": PID, "why": "조용해서 좋아요", "answers": ANS}).json()
        r2 = self.c.post("/api/saved", json={"place_id": PID, "why": "다른 문구", "answers": ANS}).json()
        self.assertTrue(r1["created"]); self.assertFalse(r2["created"])
        self.assertEqual(r1["item"]["id"], r2["item"]["id"])
        items = self.c.get("/api/saved").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["why"], "조용해서 좋아요")
        self.assertEqual(items[0]["place"]["name"], "석조전")
        self.assertNotIn("tags", items[0]["place"])
        self.assertEqual(len(self.c.get("/api/saved?palace=덕수궁").json()["items"]), 1)
        self.assertEqual(len(self.c.get("/api/saved?palace=경복궁").json()["items"]), 0)

    def test_visit_memo_and_unsave(self):
        signup(self.c)
        sid = self.c.post("/api/saved", json={"place_id": PID}).json()["item"]["id"]
        it = self.c.patch(f"/api/saved/{sid}", json={"visited": True, "memo": "좋았다\x00<b>x</b>"}).json()["item"]
        self.assertTrue(it["visited"]); self.assertTrue(it["visited_at"])
        self.assertNotIn("<", it["memo"]); self.assertNotIn("\x00", it["memo"])
        self.assertEqual(self.c.patch(f"/api/saved/{sid}", json={"memo": "x" * 501}).status_code, 422)
        it = self.c.patch(f"/api/saved/{sid}", json={"visited": False}).json()["item"]
        self.assertFalse(it["visited"]); self.assertIsNone(it["visited_at"])
        self.assertEqual(self.c.delete(f"/api/saved/{sid}").status_code, 200)
        self.assertEqual(self.c.get("/api/saved").json()["items"], [])

    def test_cannot_touch_other_users_items(self):
        signup(self.c)
        sid = self.c.post("/api/saved", json={"place_id": PID}).json()["item"]["id"]
        other = fresh()
        signup(other, email="b@example.com")
        self.assertEqual(other.patch(f"/api/saved/{sid}", json={"memo": "hack"}).status_code, 404)
        self.assertEqual(other.delete(f"/api/saved/{sid}").status_code, 404)
        self.assertEqual(other.get("/api/saved").json()["items"], [])
        self.assertEqual(self.c.get("/api/saved").json()["items"][0]["memo"], "")

    def test_history_and_last_answers(self):
        signup(self.c)
        self.assertIsNone(self.c.get("/api/assessments/last-answers").json()["answers"])
        self.c.post("/api/assess", json={"answers": ANS, "use_llm": False})
        self.c.post("/api/assess", json={"answers": ANS, "use_llm": False})  # 같은 답: 중복 저장 없음
        self.c.post("/api/assess", json={"answers": ANS2, "use_llm": False})
        items = self.c.get("/api/assessments").json()["items"]
        self.assertEqual(len(items), 2)
        la = self.c.get("/api/assessments/last-answers").json()["answers"]
        self.assertTrue(all(k.startswith("t") for k in la)); self.assertEqual(len(la), 10)
        self.assertEqual(set(la.values()), {"c"})

    def test_anonymous_assess_not_stored(self):
        self.c.post("/api/assess", json={"answers": ANS, "use_llm": False})
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 0)

    def test_delete_account_removes_everything(self):
        signup(self.c)
        self.c.post("/api/saved", json={"place_id": PID, "answers": ANS})
        self.assertEqual(self.c.delete("/api/me").status_code, 200)
        with db.conn() as c:
            for t in ("users", "sessions", "assessments", "saved_places"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0, t)
        self.assertIsNone(self.c.get("/api/me").json()["user"])

    def test_csrf_foreign_origin_blocked(self):
        signup(self.c)
        r = self.c.post("/api/saved", json={"place_id": PID}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        ok = self.c.post("/api/saved", json={"place_id": PID}, headers={"Origin": "http://testserver"})
        self.assertEqual(ok.status_code, 200)

    def test_db_file_private(self):
        mode = stat.S_IMODE(os.stat(db.db_path()).st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_tampered_answers_do_not_store_profile(self):
        signup(self.c)
        self.c.post("/api/saved", json={"place_id": PID, "answers": {"m1": "zzz"}})
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 0)

    def test_huge_id_is_404_or_422_not_500(self):
        signup(self.c)
        for m in ("patch", "delete"):
            r = getattr(self.c, m)("/api/saved/99999999999999999999999", **({"json": {"memo": "x"}} if m == "patch" else {}))
            self.assertIn(r.status_code, (404, 422))

    def test_duplicate_save_does_not_touch_history(self):
        signup(self.c)
        self.c.post("/api/saved", json={"place_id": PID, "answers": ANS, "summary": "첫 요약이에요"})
        self.c.post("/api/saved", json={"place_id": PID, "answers": ANS2})
        items = self.c.get("/api/assessments").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["summary"], "첫 요약이에요")

    def test_cross_site_without_origin_blocked_and_chunked_rejected(self):
        signup(self.c)
        r = self.c.post("/api/saved", json={"place_id": PID}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/saved", content=b'{"place_id":"x"}', headers={"Transfer-Encoding": "chunked", "content-type": "application/json"})
        self.assertEqual(r.status_code, 411)

    def test_email_with_angle_brackets_rejected(self):
        self.assertEqual(signup(self.c, email="<script>@x.com").status_code, 422)


if __name__ == "__main__":
    unittest.main()
