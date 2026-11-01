"""레지스트리 기록 테스트 — 네트워크 없이 requests 를 가짜로 바꿔 검증. 실행: python -m unittest discover tests"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_TOKEN", "x")
import registry  # noqa: E402
import publish  # noqa: E402

KST = registry.KST


class Resp:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, str(data)

    def json(self):
        return self._d


def page(text):
    return Resp(200, {"properties": {"현재 상태": {"rich_text": [{"plain_text": text}]}}})


class FakeNotion:
    def __init__(self, prev="", get_code=200, patch_code=200):
        self.prev, self.get_code, self.patch_code, self.patched = prev, get_code, patch_code, []

    def get(self, url, **kw):
        if self.get_code != 200:
            return Resp(self.get_code, {"code": "object_not_found"})
        return page(self.prev)

    def patch(self, url, json=None, **kw):
        self.patched.append((url, json))
        return Resp(self.patch_code, {"code": "restricted_resource"} if self.patch_code != 200 else {})


class BuildLine(unittest.TestCase):
    def test_format(self):
        now = dt.datetime(2026, 11, 2, 7, 5, tzinfo=KST)
        s = registry.build_line(now, "아침(인스타)", True, None, 2, 3, "https://www.instagram.com/p/X/", [])
        self.assertEqual(s, "2026-11-02 07:05 KST 자동확인 · 이번 게시 아침(인스타) 인스타 성공 스레드 해당없음"
                            " · 오늘 누적 인스타 2 · 스레드 3 · 최근 https://www.instagram.com/p/X/ · 이상: 없음")

    def test_no_permalink_is_fail(self):
        now = dt.datetime(2026, 11, 2, 21, 0, tzinfo=KST)
        s = registry.build_line(now, "밤(스레드)", None, False, None, 0, None, ["Threads: code 190", "Threads: code 190"])
        self.assertIn("스레드 실패", s)
        self.assertIn("최근 실패", s)
        self.assertIn("오늘 누적 인스타 확인불가", s)
        self.assertTrue(s.endswith("이상: Threads: code 190"))  # 중복 제거


class TokenIssue(unittest.TestCase):
    def test_insert_into_ok_line(self):
        prev = "2026-11-02 07:05 KST 자동확인 · 이번 게시 아침(인스타) 인스타 성공 스레드 해당없음 · 최근 u · 이상: 없음"
        s = registry.with_token_issue(prev, "토큰 갱신 실패 2026-11-02")
        self.assertTrue(s.endswith("· 이상: 토큰 갱신 실패 2026-11-02"))
        self.assertTrue(s.startswith("2026-11-02 07:05 KST 자동확인 · 이번 게시"))

    def test_append_and_no_dup(self):
        prev = "x · 이상: 토큰 갱신 실패 2026-10-26 / IG 캐러셀: boom"
        s = registry.with_token_issue(prev, "토큰 갱신 실패 2026-11-02")
        self.assertEqual(s, "x · 이상: 토큰 갱신 실패 2026-11-02 / IG 캐러셀: boom")

    def test_carry_window(self):
        prev = "… · 이상: 토큰 갱신 실패 2026-11-02"
        self.assertEqual(registry.carried_token_issue(prev, dt.date(2026, 11, 5)), "토큰 갱신 실패 2026-11-02")
        self.assertIsNone(registry.carried_token_issue(prev, dt.date(2026, 11, 9)))
        self.assertIsNone(registry.carried_token_issue("이상: 없음", dt.date(2026, 11, 5)))


class CountToday(unittest.TestCase):
    def test_counts_kst_today_and_stops(self):
        data = {"data": [{"timestamp": "2026-11-01T15:30:00+0000"},   # 11/2 00:30 KST
                         {"timestamp": "2026-11-01T22:00:00+0000"},   # 11/2 07:00 KST
                         {"timestamp": "2026-11-01T14:59:00+0000"}],  # 11/1 23:59 KST
                "paging": {"next": "https://next"}}
        with mock.patch.object(registry.requests, "get", return_value=Resp(200, data)) as g:
            self.assertEqual(registry.count_today("u", {}, dt.date(2026, 11, 2)), 2)
            self.assertEqual(g.call_count, 1)  # 어제 글이 보이면 다음 쪽 안 감

    def test_api_error_raises(self):
        with mock.patch.object(registry.requests, "get", return_value=Resp(400, {"error": {"message": "bad", "code": 190}})):
            with self.assertRaises(RuntimeError):
                registry.count_today("u", {}, dt.date(2026, 11, 2))


class ReportPublish(unittest.TestCase):
    def test_writes_only_status_prop(self):
        fake = FakeNotion(prev="old · 이상: 토큰 갱신 실패 " + dt.datetime.now(KST).date().isoformat())
        with mock.patch.object(registry.requests, "get", fake.get), mock.patch.object(registry.requests, "patch", fake.patch):
            line = registry.report_publish("밤(스레드)", None, True, "https://www.threads.com/@crossing_today/post/A", [],
                                           lambda d: 1, lambda d: 4, log=lambda m: None)
        self.assertEqual(len(fake.patched), 1)
        props = fake.patched[0][1]["properties"]
        self.assertEqual(list(props), ["현재 상태"])
        self.assertIn("스레드 성공", line)
        self.assertIn("오늘 누적 인스타 1 · 스레드 4", line)
        self.assertIn("이상: 토큰 갱신 실패", line)

    def test_permission_error_swallowed(self):
        logs = []
        fake = FakeNotion(get_code=404)
        with mock.patch.object(registry.requests, "get", fake.get), mock.patch.object(registry.requests, "patch", fake.patch):
            self.assertIsNone(registry.report_publish("아침(인스타)", True, None, "u", [], log=logs.append))
        self.assertTrue(any("연결 추가 필요" in m for m in logs))

    def test_count_failure_becomes_issue(self):
        fake = FakeNotion()
        def boom(d):
            raise RuntimeError("code 190")
        with mock.patch.object(registry.requests, "get", fake.get), mock.patch.object(registry.requests, "patch", fake.patch):
            line = registry.report_publish("아침(인스타)", True, None, "u", [], boom, None, log=lambda m: None)
        self.assertIn("인스타 확인불가", line)
        self.assertIn("인스타 누적 집계 실패(code 190)", line)


class PublishIntegration(unittest.TestCase):
    def setUp(self):
        publish.RUN.update({"ig": None, "th": None, "links": [], "issues": []})

    def test_mark_without_link_is_failure(self):
        publish.mark("ig", True, None)
        self.assertFalse(publish.RUN["ig"])
        self.assertIn("permalink", publish.RUN["issues"][0])

    def test_no_attempt_no_write(self):
        with mock.patch.object(registry, "report_publish") as rp:
            self.assertIsNone(publish.report_registry("아침(인스타)"))
            rp.assert_not_called()

    def test_registry_crash_does_not_raise(self):
        publish.mark("th", True, "https://t/1")
        with mock.patch.object(registry, "report_publish", side_effect=RuntimeError("x")), \
             mock.patch.object(publish, "DRY", False):
            self.assertIsNone(publish.report_registry("밤(스레드)"))

    def test_publish_row_flow(self):
        row = {"id": "p", "title": "t", "caption": "c", "threads_caption": "", "folder": "f", "music_kw": "",
               "reel_on": False, "reel_only": False, "already": None, "already_reel": None, "already_th": None}
        env = {"IG_USER_ID": "1", "FB_PAGE_TOKEN": "tok", "TH_USER_ID": "2", "TH_ACCESS_TOKEN": "t2", "DRY_RUN": "false"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(publish, "media_urls", return_value=(["a", "b"], None)), \
             mock.patch.object(publish, "ig_carousel", return_value="https://www.instagram.com/p/OK/"), \
             mock.patch.object(publish, "th_post", side_effect=publish.ApiError("Invalid OAuth (code 190)")), \
             mock.patch.object(publish, "update_row"):
            publish.publish_row(row, {"ig", "threads"})
        self.assertTrue(publish.RUN["ig"])
        self.assertFalse(publish.RUN["th"])
        self.assertEqual(publish.RUN["links"], ["https://www.instagram.com/p/OK/"])
        with mock.patch.object(publish, "DRY", False), mock.patch.object(registry, "report_publish", return_value="L") as rp:
            publish.report_registry(publish.slot_label({"ig", "threads"}))
        a = rp.call_args.args
        self.assertEqual(a[:4], ("수동(인스타+스레드)", True, False, "https://www.instagram.com/p/OK/"))
        self.assertIn("Threads: Invalid OAuth (code 190)", a[4])


if __name__ == "__main__":
    unittest.main()
