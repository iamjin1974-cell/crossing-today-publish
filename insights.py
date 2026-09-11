#!/usr/bin/env python3
"""
크로싱투데이 성과 수집기 — GitHub Actions에서 매일 23:00 KST 실행 (v1, 2026-09-11)

하는 일
  1. 노션 시트에서 최근 INSIGHTS_DAYS(기본 45)일 안에 게시된 행(게시 URL 또는 스레드 URL 있음)을 가져온다
  2. 인스타 미디어 목록(/{IG_USER_ID}/media)과 스레드 게시물 목록(/{TH_USER_ID}/threads)을 받아 permalink 로 행과 짝을 맞춘다
  3. 인스타 캐러셀·릴스 insights(조회·도달·저장·공유·좋아요·댓글·팔로우), 스레드 insights(조회·좋아요·답글·리포스트)를 행에 기록
     → 노션 열: IG 조회수(캐러셀; '조회수' 열은 원본 아웃라이어 조회수라 건드리지 않음), IG 도달, IG 저장, IG 공유, IG 좋아요, IG 댓글, IG 팔로우, 릴스 조회수, TH 조회수, TH 좋아요, TH 답글, TH 리포스트, 지표 갱신
  4. 최근 48시간 인스타 댓글·스레드 답글을 모아 노션 페이지 "오늘의 답글 YYYY-MM-DD" 를 만든다 (REPLIES_PARENT_PAGE_ID 필요)
     — 답글 초안은 이 페이지를 읽는 Claude 작업이 채운다. 여기서는 목록만.
  5. 일요일이면 페이지 상단에 주간 요약(표지 A/B 중앙값, 형식별 평균, 채널별 합계)을 붙인다

환경변수
  NOTION_TOKEN, NOTION_DATA_SOURCE_ID, REPLIES_PARENT_PAGE_ID(선택)
  IG_USER_ID, FB_PAGE_TOKEN (권장; 없으면 IG_ACCESS_TOKEN + graph.instagram.com)
  TH_USER_ID, TH_ACCESS_TOKEN
  INSIGHTS_DAYS(기본 45), DRY_RUN
"""
import json, os, re, sys, statistics, datetime as dt
import requests

IG_HOST_IGLOGIN = "https://graph.instagram.com/v25.0"
IG_HOST_FBLOGIN = "https://graph.facebook.com/v25.0"
TH_HOST = "https://graph.threads.net/v1.0"
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
TIMEOUT = 60
KST = dt.timezone(dt.timedelta(hours=9))


def env(k, d=""):
    return os.environ.get(k, d).strip()


def flag(k, d="false"):
    return env(k, d).lower() in ("1", "true", "yes", "y")


DRY = flag("DRY_RUN")


def log(m):
    print(m, flush=True)


class ApiError(Exception):
    pass


def get(url, **params):
    r = requests.get(url, params=params, timeout=TIMEOUT)
    try:
        d = r.json()
    except ValueError:
        raise ApiError(f"{url} → HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400 or "error" in d:
        e = d.get("error", {})
        raise ApiError(f"{e.get('message') or d} (code {e.get('code')}, sub {e.get('error_subcode')})")
    return d


def paged(url, limit_items=200, **params):
    out, nxt = [], None
    while len(out) < limit_items:
        d = get(url, **params) if not nxt else requests.get(nxt, timeout=TIMEOUT).json()
        out += d.get("data", [])
        nxt = (d.get("paging") or {}).get("next")
        if not nxt:
            break
    return out


def ig_credentials():
    fb = env("FB_PAGE_TOKEN")
    if fb:
        return IG_HOST_FBLOGIN, fb
    return IG_HOST_IGLOGIN, env("IG_ACCESS_TOKEN")


# ---------------- 인스타 ----------------

IG_CODE = re.compile(r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)")
TH_CODE = re.compile(r"threads\.(?:com|net)/@[^/]+/post/([A-Za-z0-9_-]+)")


def ig_media_index(host, uid, token, since):
    items = paged(f"{host}/{uid}/media", limit_items=120, fields="id,permalink,media_type,media_product_type,timestamp",
                  limit="50", access_token=token)
    idx = {}
    for m in items:
        ts = dt.datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
        if ts < since:
            continue
        code = IG_CODE.search(m.get("permalink", ""))
        if code:
            idx[code.group(1)] = m
    return idx


def ig_insights(host, media, token):
    """캐러셀/사진: views,reach,saved,shares,likes,comments,follows / 릴스: views,reach,saved,shares,likes,comments"""
    is_reel = media.get("media_product_type") == "REELS" or media.get("media_type") == "VIDEO"
    metrics = ["views", "reach", "saved", "shares", "likes", "comments"] + ([] if is_reel else ["follows"])
    out = {}
    try:
        d = get(f"{host}/{media['id']}/insights", metric=",".join(metrics), access_token=token)
        for row in d.get("data", []):
            vals = row.get("values") or []
            v = vals[0].get("value") if vals else row.get("total_value", {}).get("value")
            out[row["name"]] = v
    except ApiError as e:
        log(f"   IG insights 묶음 실패({e}) → 개별 재시도")
        for m in metrics:
            try:
                d = get(f"{host}/{media['id']}/insights", metric=m, access_token=token)
                row = (d.get("data") or [{}])[0]
                vals = row.get("values") or []
                out[m] = vals[0].get("value") if vals else None
            except ApiError:
                out[m] = None
    return out


def ig_comments(host, media, token, since):
    try:
        items = paged(f"{host}/{media['id']}/comments", limit_items=100,
                      fields="id,text,username,timestamp,replies{username,text,timestamp}", limit="50", access_token=token)
    except ApiError as e:
        log(f"   댓글 조회 실패: {e}")
        return []
    me = env("IG_USERNAME", "crossing_today")
    out = []
    for c in items:
        ts = dt.datetime.fromisoformat(c["timestamp"].replace("+0000", "+00:00"))
        if ts < since or c.get("username") == me:
            continue
        answered = any(r.get("username") == me for r in (c.get("replies") or {}).get("data", []))
        out.append({"platform": "IG", "user": c.get("username"), "text": c.get("text", ""), "ts": ts, "answered": answered,
                    "link": media.get("permalink")})
    return out


# ---------------- 스레드 ----------------

def th_index(uid, token, since):
    items = paged(f"{TH_HOST}/{uid}/threads", limit_items=120, fields="id,permalink,timestamp,media_type,is_reply",
                  limit="50", access_token=token)
    idx = {}
    for m in items:
        if m.get("is_reply"):
            continue
        ts = dt.datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
        if ts < since:
            continue
        code = TH_CODE.search(m.get("permalink", ""))
        if code:
            idx[code.group(1)] = m
    return idx


def th_insights(media, token):
    out = {}
    try:
        d = get(f"{TH_HOST}/{media['id']}/insights", metric="views,likes,replies,reposts,quotes", access_token=token)
        for row in d.get("data", []):
            vals = row.get("values") or []
            out[row["name"]] = vals[0].get("value") if vals else row.get("total_value", {}).get("value")
    except ApiError as e:
        log(f"   TH insights 실패: {e}")
    return out


def th_replies(media, token, since):
    try:
        items = paged(f"{TH_HOST}/{media['id']}/replies", limit_items=100, fields="id,text,username,timestamp,has_replies",
                      limit="50", access_token=token)
    except ApiError as e:
        log(f"   스레드 답글 조회 실패(권한 threads_manage_replies 필요할 수 있음): {e}")
        return []
    me = env("TH_USERNAME", "crossing_today")
    out = []
    for c in items:
        ts = dt.datetime.fromisoformat(c["timestamp"].replace("+0000", "+00:00"))
        if ts < since or c.get("username") == me:
            continue
        out.append({"platform": "TH", "user": c.get("username"), "text": c.get("text", ""), "ts": ts, "answered": False,
                    "link": media.get("permalink")})
    return out


def th_account(uid, token):
    try:
        d = get(f"{TH_HOST}/{uid}/threads_insights", metric="followers_count", access_token=token)
        for row in d.get("data", []):
            if row["name"] == "followers_count":
                return row.get("total_value", {}).get("value")
    except ApiError as e:
        log(f"   TH followers 조회 실패: {e}")
    return None


def ig_account(host, uid, token):
    try:
        d = get(f"{host}/{uid}", fields="followers_count,media_count", access_token=token)
        return d.get("followers_count")
    except ApiError as e:
        log(f"   IG followers 조회 실패: {e}")
        return None


# ---------------- Notion ----------------

def nh():
    return {"Authorization": f"Bearer {env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}


def rich(p):
    return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))


def title(p):
    return "".join(t.get("plain_text", "") for t in p.get("title", []))


def fetch_posted_rows():
    body = {"filter": {"or": [{"property": "게시 URL", "url": {"is_not_empty": True}},
                              {"property": "스레드 URL", "url": {"is_not_empty": True}}]},
            "page_size": 100}
    r = requests.post(f"{NOTION}/data_sources/{env('NOTION_DATA_SOURCE_ID')}/query", headers=nh(), json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise ApiError(f"노션 조회 실패 {r.status_code}: {r.text[:300]}")
    rows = []
    for pg in r.json().get("results", []):
        P = pg["properties"]
        rows.append({"id": pg["id"], "title": title(P.get("후킹 제목", {})),
                     "ig": (P.get("게시 URL", {}) or {}).get("url") or "",
                     "reel": (P.get("릴스 게시 URL", {}) or {}).get("url") or "",
                     "th": (P.get("스레드 URL", {}) or {}).get("url") or "",
                     "cover": ((P.get("표지", {}) or {}).get("select") or {}).get("name") or "",
                     "format": rich(P.get("포맷", {})),
                     "date": ((P.get("게시 예정일", {}) or {}).get("date") or {}).get("start") or pg.get("created_time", "")[:10]})
    return rows


def update_row(page_id, props):
    if DRY:
        log(f"   [DRY] 노션 갱신 {json.dumps(props, ensure_ascii=False)[:200]}")
        return
    r = requests.patch(f"{NOTION}/pages/{page_id}", headers=nh(), json={"properties": props}, timeout=TIMEOUT)
    if r.status_code >= 400:
        log(f"   노션 갱신 실패 {r.status_code}: {r.text[:200]}")


def num(v):
    try:
        return {"number": int(v)} if v is not None else None
    except (TypeError, ValueError):
        return None


def para(text):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": text[:1900]}}]}}


def heading(text):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": text[:200]}}]}}


def create_page(parent_id, ptitle, blocks):
    if DRY:
        log(f"   [DRY] 페이지 생성 '{ptitle}' 블록 {len(blocks)}개")
        return
    body = {"parent": {"page_id": parent_id}, "properties": {"title": {"title": [{"text": {"content": ptitle}}]}},
            "children": blocks[:100]}
    r = requests.post(f"{NOTION}/pages", headers=nh(), json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        log(f"   페이지 생성 실패 {r.status_code}: {r.text[:300]}")
        return None
    pid = r.json()["id"]
    rest = blocks[100:]
    while rest:
        requests.patch(f"{NOTION}/blocks/{pid}/children", headers=nh(), json={"children": rest[:100]}, timeout=TIMEOUT)
        rest = rest[100:]
    return pid


# ---------------- 메인 ----------------

def main():
    now = dt.datetime.now(KST)
    log(f"성과 수집 시작 {now:%Y-%m-%d %H:%M} KST DRY={DRY}")
    since = (now - dt.timedelta(days=int(env("INSIGHTS_DAYS", "45")))).astimezone(dt.timezone.utc)
    since48 = (now - dt.timedelta(hours=48)).astimezone(dt.timezone.utc)
    host, ig_tok = ig_credentials()
    ig_uid, th_uid, th_tok = env("IG_USER_ID"), env("TH_USER_ID"), env("TH_ACCESS_TOKEN")

    ig_idx = ig_media_index(host, ig_uid, ig_tok, since) if ig_uid and ig_tok else {}
    th_idx = th_index(th_uid, th_tok, since) if th_uid and th_tok else {}
    log(f"인스타 미디어 {len(ig_idx)}개, 스레드 게시물 {len(th_idx)}개 (최근 {env('INSIGHTS_DAYS', '45')}일)")

    rows = fetch_posted_rows()
    comments, table = [], []
    for row in rows:
        props, rec = {}, {"title": row["title"], "cover": row["cover"], "format": row["format"], "date": row["date"]}
        m = IG_CODE.search(row["ig"])
        if m and m.group(1) in ig_idx:
            media = ig_idx[m.group(1)]
            ins = ig_insights(host, media, ig_tok)
            rec["ig_views"] = ins.get("views")
            for key, col in (("views", "IG 조회수"), ("reach", "IG 도달"), ("saved", "IG 저장"), ("shares", "IG 공유"),
                             ("likes", "IG 좋아요"), ("comments", "IG 댓글"), ("follows", "IG 팔로우")):
                v = num(ins.get(key))
                if v:
                    props[col] = v
            comments += ig_comments(host, media, ig_tok, since48)
        m = IG_CODE.search(row["reel"])
        if m and m.group(1) in ig_idx:
            media = ig_idx[m.group(1)]
            ins = ig_insights(host, media, ig_tok)
            v = num(ins.get("views"))
            if v:
                props["릴스 조회수"] = v
            comments += ig_comments(host, media, ig_tok, since48)
        m = TH_CODE.search(row["th"]) or TH_CODE.search(row["ig"])  # 옛 행은 게시 URL 칸에 "IG | threads" 로 같이 적혀 있음
        if m and m.group(1) in th_idx:
            media = th_idx[m.group(1)]
            ins = th_insights(media, th_tok)
            rec["th_views"] = ins.get("views")
            for key, col in (("views", "TH 조회수"), ("likes", "TH 좋아요"), ("replies", "TH 답글"), ("reposts", "TH 리포스트")):
                v = num(ins.get(key))
                if v:
                    props[col] = v
            comments += th_replies(media, th_tok, since48)
        if props:
            props["지표 갱신"] = {"date": {"start": now.date().isoformat()}}
            update_row(row["id"], props)
            log(f" ✓ {row['title'][:30]}  IG {rec.get('ig_views')}  TH {rec.get('th_views')}")
            table.append(rec)

    # ---- 오늘의 답글 페이지 ----
    parent = env("REPLIES_PARENT_PAGE_ID")
    if parent:
        blocks = []
        ig_f, th_f = ig_account(host, ig_uid, ig_tok), th_account(th_uid, th_tok)
        blocks.append(para(f"팔로워 — 인스타 {ig_f if ig_f is not None else '?'} · 스레드 {th_f if th_f is not None else '?'}   (수집 {now:%m/%d %H:%M})"))
        if now.weekday() == 6 and table:
            blocks.append(heading("주간 요약"))
            blocks += weekly_summary(table, now)
        blocks.append(heading(f"답글 필요 {len([c for c in comments if not c['answered']])}건 / 전체 {len(comments)}건 (48시간)"))
        if not comments:
            blocks.append(para("새 댓글·답글 없음."))
        for c in sorted(comments, key=lambda x: x["ts"], reverse=True):
            mark = "✅ 답함" if c["answered"] else "⬜ 답글 필요"
            blocks.append(para(f"[{c['platform']}] {mark} @{c['user']} ({c['ts'].astimezone(KST):%m/%d %H:%M}) — {c['text'][:300]}\n{c['link']}"))
            blocks.append(para("→ 답글 초안: "))
        create_page(parent, f"오늘의 답글 {now:%Y-%m-%d}", blocks)
        log(f"오늘의 답글 페이지: 댓글 {len(comments)}건")
    return 0


def weekly_summary(table, now):
    """최근 14일 행으로 표지 A/B · 형식별 · 채널별 요약"""
    cutoff = (now - dt.timedelta(days=14)).date().isoformat()
    recent = [t for t in table if (t.get("date") or "") >= cutoff]
    out = []

    def med(xs):
        xs = [x for x in xs if isinstance(x, (int, float))]
        return f"{statistics.median(xs):.0f} (n={len(xs)})" if xs else "—"

    for label, key in (("인스타 캐러셀 조회 중앙값", "ig_views"), ("스레드 조회 중앙값", "th_views")):
        out.append(para(f"{label}: 전체 {med([t.get(key) for t in recent])} · 표지 A 실루엣 {med([t.get(key) for t in recent if t['cover'].startswith('A')])} · 표지 B 글자 {med([t.get(key) for t in recent if t['cover'].startswith('B')])}"))
    fmts = {}
    for t in recent:
        fmts.setdefault(t.get("format") or "(미기재)", []).append(t)
    for f, ts in fmts.items():
        out.append(para(f"형식 '{f}': 인스타 {med([t.get('ig_views') for t in ts])} · 스레드 {med([t.get('th_views') for t in ts])}"))
    top = sorted(recent, key=lambda t: (t.get("th_views") or 0), reverse=True)[:3]
    if top:
        out.append(para("스레드 상위 3: " + " / ".join(f"{t['title'][:18]}({t.get('th_views')})" for t in top)))
    return out


if __name__ == "__main__":
    sys.exit(main())
