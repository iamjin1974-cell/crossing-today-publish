#!/usr/bin/env python3
"""크로싱투데이 댓글 자동 답글 (v1, 2026-10-17)

publish.py 의 텍스트 단계(30분마다 도는 schedule 실행)가 끝날 때 함께 실행된다.
  1) 수집: 최근 COMMENT_DAYS(기본 4)일 스레드 게시물의 답글 + 인스타 게시물 댓글을 읽어
     노션 '크로싱투데이 댓글 큐'에 새 것만 '대기'로 넣는다 (댓글ID 로 중복 방지).
  2) 게시: 큐에서 상태 '답글 준비' 이고 '답글' 칸이 채워진 행을 실제로 답글 게시 → '완료' (실패 시 '실패' + 로그).
답글 문장은 여기서 만들지 않는다. Claude 예약 작업 '크로싱투데이 댓글 답글'이 '대기' 행을 읽고 '답글'을 채운 뒤 '답글 준비'로 바꾼다.
외부 AI API·추가 비용 없음.

환경변수: NOTION_TOKEN, IG_USER_ID, FB_PAGE_TOKEN|IG_ACCESS_TOKEN, TH_USER_ID, TH_ACCESS_TOKEN,
          COMMENT_QUEUE_DS(기본 아래 값), COMMENT_DAYS, REPLIES_ENABLED(기본 true), TH_USERNAME, IG_USERNAME
"""
import datetime as dt
import requests

import publish as P

QUEUE_DS = P.env("COMMENT_QUEUE_DS", "6e2c107b-0ab6-4bcf-90f2-6a9f3bb1895e")
MAX_POST_PER_RUN = 15


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("+0000", "+00:00"))


def _get(url, **params):
    return P.call("GET", url, **params)


def _paged(url, limit_items=100, **params):
    out, nxt = [], None
    while True:
        d = requests.get(nxt, timeout=P.TIMEOUT).json() if nxt else _get(url, **params)
        out += d.get("data", [])
        nxt = (d.get("paging") or {}).get("next")
        if not nxt or len(out) >= limit_items:
            return out[:limit_items]


# ---------------- 노션 큐 ----------------

def q_query(filter_=None, page_size=100):
    body = {"page_size": page_size, "sorts": [{"timestamp": "created_time", "direction": "descending"}]}
    if filter_:
        body["filter"] = filter_
    r = requests.post(f"{P.NOTION}/data_sources/{QUEUE_DS}/query", headers=P.nh(), json=body, timeout=P.TIMEOUT)
    if r.status_code >= 400:
        raise P.ApiError(f"댓글 큐 조회 실패 {r.status_code}: {r.text[:300]}")
    return r.json().get("results", [])


def q_add(c):
    props = {
        "댓글": {"title": [{"text": {"content": (c["text"] or "(내용 없음)")[:1900]}}]},
        "댓글ID": P.txt(c["id"]),
        "플랫폼": {"select": {"name": c["platform"]}},
        "작성자": P.txt(c["user"] or ""),
        "게시물": {"url": c.get("link") or None},
        "작성시각": {"date": {"start": c["ts"].isoformat()}},
        "상태": {"select": {"name": "대기"}},
    }
    if P.DRY:
        P.log(f"[DRY] 큐 추가 {c['platform']} @{c['user']}: {c['text'][:40]}")
        return
    r = requests.post(f"{P.NOTION}/pages", headers=P.nh(),
                      json={"parent": {"type": "data_source_id", "data_source_id": QUEUE_DS}, "properties": props}, timeout=P.TIMEOUT)
    if r.status_code >= 400:
        P.log(f"  큐 추가 실패 {r.status_code}: {r.text[:200]}")


def q_update(page_id, status, logline):
    stamp = dt.datetime.now(P.KST).strftime("%m/%d %H:%M")
    P.update_row(page_id, {"상태": {"select": {"name": status}}, "로그": P.txt(f"[{stamp}] {logline}")})


# ---------------- 수집 ----------------

def collect_threads(since):
    uid, tok = P.env("TH_USER_ID"), P.env("TH_ACCESS_TOKEN")
    if not (uid and tok):
        return []
    me = P.env("TH_USERNAME", "crossing_today")
    posts = _paged(f"{P.TH_HOST}/{uid}/threads", limit_items=40, fields="id,permalink,timestamp,is_reply", limit="40", access_token=tok)
    out = []
    for m in posts:
        if m.get("is_reply") or _ts(m["timestamp"]) < since:
            continue
        try:
            reps = _paged(f"{P.TH_HOST}/{m['id']}/replies", limit_items=100,
                          fields="id,text,username,timestamp", limit="50", access_token=tok)
        except P.ApiError as e:
            P.log(f"  스레드 답글 조회 실패: {e}")
            continue
        for c in reps:
            if c.get("username") == me or _ts(c["timestamp"]) < since:
                continue
            out.append({"platform": "TH", "id": c["id"], "user": c.get("username"), "text": c.get("text", ""),
                        "ts": _ts(c["timestamp"]), "link": m.get("permalink")})
    return out


def collect_ig(since):
    uid = P.env("IG_USER_ID")
    host, tok, _ = P.ig_credentials()
    if not (uid and tok):
        return []
    me = P.env("IG_USERNAME", "crossing_today")
    media = _paged(f"{host}/{uid}/media", limit_items=20, fields="id,permalink,timestamp,comments_count", limit="20", access_token=tok)
    out = []
    for m in media:
        if _ts(m["timestamp"]) < since or not m.get("comments_count"):
            continue
        try:
            cs = _paged(f"{host}/{m['id']}/comments", limit_items=100,
                        fields="id,text,username,timestamp,replies{username}", limit="50", access_token=tok)
        except P.ApiError as e:
            P.log(f"  인스타 댓글 조회 실패: {e}")
            continue
        for c in cs:
            if c.get("username") == me or _ts(c["timestamp"]) < since:
                continue
            if any(r.get("username") == me for r in (c.get("replies") or {}).get("data", [])):
                continue
            out.append({"platform": "IG", "id": c["id"], "user": c.get("username"), "text": c.get("text", ""),
                        "ts": _ts(c["timestamp"]), "link": m.get("permalink")})
    return out


# ---------------- 게시 ----------------

def post_reply(platform, cid, text):
    if platform == "TH":
        uid, tok = P.env("TH_USER_ID"), P.env("TH_ACCESS_TOKEN")
        c = P.call("POST", f"{P.TH_HOST}/{uid}/threads", media_type="TEXT", text=text[:500], reply_to_id=cid, access_token=tok)
        return P.th_publish(uid, tok, c["id"])
    host, tok, _ = P.ig_credentials()
    d = P.call("POST", f"{host}/{cid}/replies", message=text[:2000], access_token=tok)
    return d.get("id")


def prop_text(p):
    return P.rich(p) if "rich_text" in p else P.title(p)


def run():
    if not P.flag("REPLIES_ENABLED", "true"):
        P.log("댓글 답글: 꺼짐(REPLIES_ENABLED=false)")
        return
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(P.env("COMMENT_DAYS", "4")))
    # 1) 수집
    try:
        known = {prop_text(r["properties"].get("댓글ID", {})) for r in q_query(page_size=100)}
        found = []
        for fn in (collect_threads, collect_ig):
            try:
                found += fn(since)
            except Exception as e:
                P.log(f"  댓글 수집 오류({fn.__name__}): {e}")
        new = [c for c in found if c["id"] not in known]
        for c in sorted(new, key=lambda x: x["ts"]):
            q_add(c)
        P.log(f"댓글 수집: 발견 {len(found)} · 새로 큐에 {len(new)}")
    except Exception as e:
        P.log(f"댓글 수집 단계 실패: {e}")
    # 2) 게시
    try:
        ready = q_query({"property": "상태", "select": {"equals": "답글 준비"}}, page_size=MAX_POST_PER_RUN)
    except Exception as e:
        P.log(f"답글 큐 조회 실패: {e}")
        return
    done = 0
    for r in ready:
        pr = r["properties"]
        text = prop_text(pr.get("답글", {})).strip()
        cid = prop_text(pr.get("댓글ID", {})).strip()
        plat = ((pr.get("플랫폼", {}) or {}).get("select") or {}).get("name")
        if not (text and cid and plat):
            q_update(r["id"], "보류", "답글·댓글ID·플랫폼 중 비어 있음")
            continue
        try:
            res = post_reply(plat, cid, text)
            q_update(r["id"], "완료", f"게시 {res or ''}")
            done += 1
        except Exception as e:
            q_update(r["id"], "실패", str(e)[:500])
            P.log(f"  답글 실패 {plat} {cid}: {e}")
    if ready:
        P.log(f"답글 게시: {done}/{len(ready)}")
