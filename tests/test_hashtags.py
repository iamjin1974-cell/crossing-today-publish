import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import publish as P

def tags(s): return P.TAG_RE.findall(s)

def test_caption_mode_caps_to_5():
    os.environ.pop("HASHTAG_MODE", None)
    c = "오늘도 한 걸음\n\n#동기부여 #자기계발 #성장 #습관 #루틴 #아침 #독서 #목표"
    out = P.rewrite_hashtags(c)
    assert tags(out) == ["동기부여", "자기계발", "성장", "습관", "루틴"]
    assert out.startswith("오늘도 한 걸음")

def test_rewrite_mode_caps_to_5():
    os.environ["HASHTAG_MODE"] = "rewrite"
    try:
        out = P.rewrite_hashtags("본문\n#논어 #맹자 #장자")
        assert len(tags(out)) <= 5
    finally:
        os.environ.pop("HASHTAG_MODE", None)

def test_under_limit_unchanged():
    c = "본문 #하나 #둘"
    assert P.rewrite_hashtags(c) == c

def test_duplicates_removed_and_inline_kept():
    out = P.cap_hashtags("#a 본문 #a #b #c #d #e #f")
    assert tags(out) == ["a", "b", "c", "d", "e"]
