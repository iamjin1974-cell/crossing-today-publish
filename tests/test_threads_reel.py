"""Threads 릴스 회차 영상 게시 / 사진 회차 음악 문장 제거 — 네트워크 없음.
requests 와 time.sleep 을 막고, publish.call 은 가짜로 바꿔 어떤 외부 호출도 나가지 않게 한다."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import publish as P

CAP = "오늘도 한 걸음 나아간다. 이 노래를 들으며 걸었다.\n영상 속 음악이 마음에 남는다!\n내일은 더 걸어야지."
REEL = "https://cdn.jsdelivr.net/gh/o/r@sha/posts/87_contact/reel.mp4"
IMG = ["https://cdn.jsdelivr.net/gh/o/r@sha/posts/87_contact/slide_01.jpg"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("네트워크 호출 시도됨")
    monkeypatch.setattr(P.requests, "get", boom)
    monkeypatch.setattr(P.requests, "post", boom)
    monkeypatch.setattr(P.requests, "patch", boom)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    monkeypatch.setattr(P, "DRY", False)
    monkeypatch.delenv("THREADS_MEDIA", raising=False)
    P.LOG.clear()


class FakeCall:
    def __init__(self, fail_video_status=None, fail_publish=False):
        self.calls, self.fail_video_status, self.fail_publish = [], fail_video_status, fail_publish

    def __call__(self, method, url, **params):
        self.calls.append((method, url, params))
        if url.endswith("/threads") and method == "POST":
            return {"id": "C1"}
        if url.endswith("/C1"):
            return {"status": self.fail_video_status or "FINISHED"}
        if url.endswith("/threads_publish"):
            if self.fail_publish:
                raise P.ApiError("timeout")
            return {"id": "P1"}
        return {"permalink": "https://www.threads.net/@x/post/P1"}

    def creates(self):
        return [p for m, u, p in self.calls if m == "POST" and u.endswith("/threads")]


def test_reel_round_uses_video_request(monkeypatch):
    f = FakeCall(); monkeypatch.setattr(P, "call", f)
    url = P.th_post("U", "T", IMG, CAP, reel_url=REEL, notes=[])
    c = f.creates()
    assert len(c) == 1 and c[0]["media_type"] == "VIDEO" and c[0]["video_url"] == REEL
    assert c[0]["text"] == CAP and "image_url" not in c[0]   # 영상 게시는 캡션 원문 유지
    assert url.endswith("P1")


def test_photo_round_removes_song_sentences(monkeypatch):
    f = FakeCall(); monkeypatch.setattr(P, "call", f)
    notes = []
    P.th_post("U", "T", IMG, CAP, reel_url=None, notes=notes)
    c = f.creates()
    assert c[0]["media_type"] == "IMAGE"
    assert "노래" not in c[0]["text"] and "음악" not in c[0]["text"]
    assert "오늘도 한 걸음 나아간다." in c[0]["text"] and "내일은 더 걸어야지." in c[0]["text"]
    assert any("노래" in n for n in notes)


def test_video_failure_falls_back_to_photo_without_song(monkeypatch):
    f = FakeCall(fail_video_status="ERROR")
    # 영상 컨테이너만 ERROR, 이후 사진 컨테이너는 정상 — 첫 상태조회만 ERROR
    state = {"n": 0}
    orig = f.__call__

    def seq(method, url, **params):
        if url.endswith("/C1"):
            state["n"] += 1
            return {"status": "ERROR" if state["n"] == 1 else "FINISHED"}
        return orig(method, url, **params)
    monkeypatch.setattr(P, "call", seq)
    notes = []
    P.th_post("U", "T", IMG, CAP, reel_url=REEL, notes=notes)
    c = f.creates()
    assert [x["media_type"] for x in c] == ["VIDEO", "IMAGE"]
    assert "노래" not in c[1]["text"] and "음악" not in c[1]["text"]
    assert any("영상 실패" in n for n in notes) and any("음악 문장 제거" in n for n in notes)
    assert not any("access_token" in n for n in notes)


def test_uncertain_publish_does_not_fall_back(monkeypatch):
    f = FakeCall(fail_publish=True); monkeypatch.setattr(P, "call", f)
    with pytest.raises(P.ThreadsPublishUncertain):
        P.th_post("U", "T", IMG, CAP, reel_url=REEL, notes=[])
    assert [x["media_type"] for x in f.creates()] == ["VIDEO"]  # 사진 중복 게시 없음


def test_video_timeout_is_finite_then_photo(monkeypatch):
    monkeypatch.setenv("THREADS_VIDEO_MAX_WAIT", "30")
    clock = {"t": 0.0}
    monkeypatch.setattr(P.time, "time", lambda: clock.__setitem__("t", clock["t"] + 20) or clock["t"])
    f = FakeCall(fail_video_status="IN_PROGRESS")
    orig = f.__call__
    def seq(method, url, **params):
        if url.endswith("/C1") and f.fail_video_status and any(p.get("media_type") == "IMAGE" for p in f.creates()):
            return {"status": "FINISHED"}
        return orig(method, url, **params)
    monkeypatch.setattr(P, "call", seq)
    P.th_post("U", "T", IMG, CAP, reel_url=REEL, notes=[])
    assert [x["media_type"] for x in f.creates()] == ["VIDEO", "IMAGE"]


def test_scrub_hides_secrets():
    s = P.scrub("fail access_token=ABC123 https://x.y/z?a=1 chat_id=99 Bearer abcdef")
    for bad in ("ABC123", "https://", "99", "abcdef"):
        assert bad not in s


def test_unrelated_sentences_preserved():
    out, removed = P.strip_music_sentences("곡선을 그리며 걷는다.\n#동기부여 #음악\n굴곡진 하루.")
    assert removed == [] and "곡선" in out and "#음악" in out
