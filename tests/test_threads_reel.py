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


# ---- 2차 보완 (2026-11-15) ----

@pytest.mark.parametrize("sent", ["내 이야기를 들어봐요.", "이웃의 목소리에 귀 기울여요.", "가사 노동을 나눕니다.",
                                  "바람 소리가 들려요.", "친구가 내 부탁을 들어줬다."])
def test_non_music_sentences_preserved(sent):
    out, removed = P.strip_music_sentences(f"오늘의 기록. {sent}\n#기록")
    assert removed == [] and sent in out


@pytest.mark.parametrize("sent", ["이 노래 가사가 마음에 남는다.", "영상 속 음악이 들려요!", "이 곡을 들어봐요."])
def test_music_sentences_removed_and_logged(monkeypatch, sent):
    out, removed = P.strip_music_sentences(f"오늘도 걷는다. {sent}")
    assert removed == [sent] and sent not in out and "오늘도 걷는다." in out
    f = FakeCall(); monkeypatch.setattr(P, "call", f)
    notes = []
    P.th_post("U", "T", IMG, f"오늘도 걷는다. {sent}", reel_url=None, notes=notes)
    assert any(sent[:6] in n for n in notes) and P.LOG and any("음악 문장" in l for l in P.LOG)


def test_wait_ready_status_fields_match_official_doc(monkeypatch):
    """공식 문서(troubleshooting): GET /{container}?fields=status,error_message, 값 IN_PROGRESS/FINISHED/PUBLISHED/ERROR/EXPIRED"""
    seen = []
    def fake(method, url, **p):
        seen.append(p.get("fields")); return {"status": "FINISHED"}
    monkeypatch.setattr(P, "call", fake)
    P.wait_ready("https://graph.threads.com/v1.0/C1", "T", fields="status,error_message", max_wait=30, every=1)
    assert seen == ["status,error_message"]
    for st in ("ERROR", "EXPIRED"):
        monkeypatch.setattr(P, "call", lambda m, u, st=st, **p: {"status": st, "error_message": "FAILED_PROCESSING_VIDEO"})
        with pytest.raises(P.ApiError):
            P.wait_ready("u", "T", fields="status,error_message", max_wait=30, every=1)
    monkeypatch.setattr(P, "call", lambda m, u, **p: {"status": "PUBLISHED"})
    P.wait_ready("u", "T", fields="status,error_message", max_wait=30, every=1)


# ---- publish_row 호출부 연결 (인스타·Notion·Threads 전부 mock) ----

def run_row(monkeypatch, row_over, reel_url, fake):
    props, ig_reel_urls = {}, []
    monkeypatch.setattr(P, "media_urls", lambda folder: (IMG, reel_url))
    monkeypatch.setattr(P, "ig_credentials", lambda: ("H", "IGT", False))
    monkeypatch.setattr(P, "ig_carousel", lambda *a, **k: "https://ig/c")
    monkeypatch.setattr(P, "ig_reel", lambda host, uid, tok, url, *a, **k: ig_reel_urls.append(url) or "https://ig/r")
    monkeypatch.setattr(P, "update_row", lambda pid, pr: props.update(pr))
    monkeypatch.setattr(P, "call", fake)
    for k, v in dict(IG_USER_ID="I", TH_USER_ID="U", TH_ACCESS_TOKEN="T", POST_REELS="true", MUSIC_ENABLED="false").items():
        monkeypatch.setenv(k, v)
    row = dict(id="p", title="t", caption=CAP, threads_caption=CAP, folder="87_contact", music_kw="none",
               reel_on=True, reel_only=False, already=None, already_reel=None, already_th=None)
    row.update(row_over)
    errs = P.publish_row(row, {"ig", "reels", "threads"})
    return errs, props, ig_reel_urls


def test_publish_row_reel_checked_uses_same_url_as_instagram_video(monkeypatch):
    f = FakeCall()
    errs, props, ig_urls = run_row(monkeypatch, {}, REEL, f)
    c = f.creates()
    assert errs == [] and ig_urls == [REEL]
    assert len(c) == 1 and c[0]["media_type"] == "VIDEO" and c[0]["video_url"] == REEL == ig_urls[0]
    assert props["스레드 URL"]["url"].endswith("P1")


def test_publish_row_reel_only_format_also_video(monkeypatch):
    f = FakeCall()
    run_row(monkeypatch, {"reel_on": False, "reel_only": True}, REEL, f)
    assert [x["media_type"] for x in f.creates()] == ["VIDEO"]


def test_publish_row_no_reel_file_falls_back_to_photo_with_note(monkeypatch):
    f = FakeCall()
    errs, props, ig_urls = run_row(monkeypatch, {}, None, f)
    c = f.creates()
    assert errs == [] and ig_urls == []
    assert [x["media_type"] for x in c] == ["IMAGE"] and "video_url" not in c[0]
    assert "노래" not in c[0]["text"] and "음악" not in c[0]["text"]
    log = props["게시 로그"]["rich_text"][0]["text"]["content"]
    assert "reel.mp4 없음" in log and "음악 문장 제거" in log


def test_publish_row_reel_unchecked_stays_photo(monkeypatch):
    f = FakeCall()
    run_row(monkeypatch, {"reel_on": False, "reel_only": False}, REEL, f)
    assert [x["media_type"] for x in f.creates()] == ["IMAGE"]


# ---- 폴링 기본값: Threads 영상 60초(공식 권장 1분당 1회, 최대 5분), 인스타 공통 wait_ready 기본은 불변 ----

def _th_video_wait_kwargs(monkeypatch):
    got = {}
    monkeypatch.setattr(P, "wait_ready", lambda url, token, **kw: got.update(kw))
    monkeypatch.setattr(P, "call", FakeCall())
    P.th_video("U", "T", REEL, "본문")
    return got


def test_th_video_default_poll_is_60s_max_300s(monkeypatch):
    monkeypatch.delenv("THREADS_VIDEO_POLL_EVERY", raising=False)
    monkeypatch.delenv("THREADS_VIDEO_MAX_WAIT", raising=False)
    kw = _th_video_wait_kwargs(monkeypatch)
    assert kw["every"] == 60 and kw["max_wait"] == 300


def test_th_video_poll_env_override(monkeypatch):
    monkeypatch.setenv("THREADS_VIDEO_POLL_EVERY", "45")
    assert _th_video_wait_kwargs(monkeypatch)["every"] == 45


def test_wait_ready_common_defaults_unchanged_for_instagram():
    import inspect
    d = inspect.signature(P.wait_ready).parameters
    assert d["max_wait"].default == 600 and d["every"].default == 15


def test_th_video_wait_is_finite_with_60s_poll(monkeypatch):
    """IN_PROGRESS 가 계속되면 유한 시간 후 ApiError (가짜 시계, 실제 sleep 없음)"""
    t = {"now": 0.0}
    monkeypatch.setattr(P.time, "time", lambda: t["now"])
    sleeps = []
    monkeypatch.setattr(P.time, "sleep", lambda s: (sleeps.append(s), t.__setitem__("now", t["now"] + s)))
    monkeypatch.setattr(P, "call", FakeCall(fail_video_status="IN_PROGRESS"))
    monkeypatch.delenv("THREADS_VIDEO_POLL_EVERY", raising=False)
    monkeypatch.delenv("THREADS_VIDEO_MAX_WAIT", raising=False)
    with pytest.raises(P.ApiError, match="시간 초과"):
        P.th_video("U", "T", REEL, "본문")
    assert set(sleeps) == {60} and len(sleeps) == 6
