#!/usr/bin/env python3
"""
크로싱투데이 자동 게시 — GitHub Actions에서 매일 실행

흐름
  1. 노션 '크로싱투데이 콘텐츠 시트'에서  승인=체크 & 상태≠완료 & (게시 예정일 비었거나 ≤ 오늘)  행을 가져온다
  2. 행의 '미디어 폴더'(예: 27_공통점다섯) 아래 posts/<폴더>/slide_*.jpg, reel.mp4 를 공개 URL로 만든다
       - Cloudinary 키가 있으면 Cloudinary에 올려 그 URL 사용
       - 없으면 이 저장소 파일의 jsDelivr CDN URL(기본) 또는 raw.githubusercontent.com URL 사용 (MEDIA_HOST, 저장소 public 필요)
         경로의 한글은 퍼센트 인코딩 (Meta 페처가 비ASCII URL을 못 읽음 — 27호 첫 실행에서 확인)
  3. Instagram 캐러셀 → (reel.mp4 있으면) Instagram 릴스 → Threads 캐러셀 순서로 게시
  4. 노션 행에 게시 URL·릴스 게시 URL·스레드 URL·게시 로그 기록, 상태 '완료'
     (이미 URL이 있는 채널은 재실행 때 건너뜀 → 일부 실패 후 재실행해도 중복 게시 없음)

환경변수 (GitHub Secrets)
  NOTION_TOKEN, NOTION_DATA_SOURCE_ID
  IG_USER_ID, IG_ACCESS_TOKEN, TH_USER_ID, TH_ACCESS_TOKEN
  CLD_CLOUD_NAME, CLD_API_KEY, CLD_API_SECRET (선택)
  GITHUB_REPOSITORY (Actions가 자동 제공), DRY_RUN, POST_REELS, POST_THREADS, MAX_POSTS_PER_RUN, MEDIA_HOST(jsdelivr|raw)
"""
import glob, hashlib, json, os, sys, time, datetime as dt
from urllib.parse import quote
import requests

IG_HOST = "https://graph.instagram.com/v25.0"
TH_HOST = "https://graph.threads.net/v1.0"
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
TIMEOUT = 60
KST = dt.timezone(dt.timedelta(hours=9))


def env(k, d=""):
    return os.environ.get(k, d).strip()


def flag(k, d="false"):
    return env(k, d).lower() in ("1", "true", "yes", "y")


DRY = flag("DRY_RUN", "false")
LOG = []


def log(m):
    LOG.append(m)
    print(m, flush=True)


class ApiError(Exception):
    pass


# ---------------- Meta 공통 ----------------

def call(method, url, **params):
    if DRY:
        safe = {k: (v[:50] + "…" if isinstance(v, str) and len(v) > 50 else v) for k, v in params.items() if k != "access_token"}
        log(f"[DRY] {method} {url} {json.dumps(safe, ensure_ascii=False)}")
        return {"id": f"DRY_{int(time.time()*1000)}", "status_code": "FINISHED", "status": "FINISHED",
                "permalink": "https://example.invalid/dry-run"}
    r = requests.get(url, params=params, timeout=TIMEOUT) if method == "GET" else requests.post(url, data=params, timeout=TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        raise ApiError(f"{url} → HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400 or "error" in data:
        e = data.get("error", {})
        raise ApiError(f"{e.get('message') or data} (code {e.get('code')}, sub {e.get('error_subcode')}) {e.get('error_user_msg', '')}".strip())
    return data


def wait_ready(url, token, fields="status_code,status", max_wait=600, every=15):
    if DRY:
        return
    t0 = time.time()
    while True:
        d = call("GET", url, fields=fields, access_token=token)
        st = d.get("status_code") or d.get("status")
        if st in ("FINISHED", "PUBLISHED"):
            return
        if st in ("ERROR", "EXPIRED"):
            raise ApiError(f"컨테이너 처리 실패: {json.dumps(d, ensure_ascii=False)}")
        if time.time() - t0 > max_wait:
            raise ApiError(f"컨테이너 처리 시간 초과, 마지막 상태 {st}")
        time.sleep(every)


def ig_carousel(uid, token, urls, caption):
    if not 2 <= len(urls) <= 10:
        raise ApiError(f"인스타 캐러셀은 2~10장 (현재 {len(urls)})")
    kids = []
    for i, u in enumerate(urls, 1):
        kids.append(call("POST", f"{IG_HOST}/{uid}/media", image_url=u, is_carousel_item="true", access_token=token)["id"])
        log(f"  IG 슬라이드 {i}/{len(urls)} 컨테이너 OK")
    c = call("POST", f"{IG_HOST}/{uid}/media", media_type="CAROUSEL", children=",".join(kids), caption=caption, access_token=token)
    wait_ready(f"{IG_HOST}/{c['id']}", token)
    p = call("POST", f"{IG_HOST}/{uid}/media_publish", creation_id=c["id"], access_token=token)
    return call("GET", f"{IG_HOST}/{p['id']}", fields="permalink", access_token=token).get("permalink")


def ig_reel(uid, token, video_url, caption, cover_url=None):
    params = dict(media_type="REELS", video_url=video_url, caption=caption, share_to_feed="true", access_token=token)
    if cover_url:
        params["cover_url"] = cover_url
    c = call("POST", f"{IG_HOST}/{uid}/media", **params)
    log("  IG 릴스 컨테이너 생성, 영상 처리 대기…")
    wait_ready(f"{IG_HOST}/{c['id']}", token)
    p = call("POST", f"{IG_HOST}/{uid}/media_publish", creation_id=c["id"], access_token=token)
    return call("GET", f"{IG_HOST}/{p['id']}", fields="permalink", access_token=token).get("permalink")


def th_carousel(uid, token, urls, text):
    if len(text) > 500:
        raise ApiError(f"스레드 본문 500자 초과 ({len(text)}자)")
    if not 2 <= len(urls) <= 20:
        raise ApiError(f"스레드 캐러셀은 2~20장 (현재 {len(urls)})")
    kids = []
    for i, u in enumerate(urls, 1):
        kids.append(call("POST", f"{TH_HOST}/{uid}/threads", media_type="IMAGE", image_url=u, is_carousel_item="true", access_token=token)["id"])
        log(f"  TH 슬라이드 {i}/{len(urls)} 컨테이너 OK")
    # 자식 컨테이너가 전부 FINISHED 되기 전에 캐러셀을 만들면 "children invalid/expired"(sub 4279004) — 27호에서 확인
    for i, k in enumerate(kids, 1):
        wait_ready(f"{TH_HOST}/{k}", token, fields="status,error_message", max_wait=300, every=10)
    log("  TH 자식 컨테이너 전부 준비됨")
    c = call("POST", f"{TH_HOST}/{uid}/threads", media_type="CAROUSEL", children=",".join(kids), text=text, access_token=token)
    wait_ready(f"{TH_HOST}/{c['id']}", token, fields="status,error_message")
    p = call("POST", f"{TH_HOST}/{uid}/threads_publish", creation_id=c["id"], access_token=token)
    return call("GET", f"{TH_HOST}/{p['id']}", fields="permalink", access_token=token).get("permalink")


# ---------------- 미디어 URL ----------------

def cld_upload(path, folder, public_id, rtype):
    cloud, key, secret = env("CLD_CLOUD_NAME"), env("CLD_API_KEY"), env("CLD_API_SECRET")
    params = {"timestamp": str(int(time.time())), "folder": folder, "public_id": public_id, "overwrite": "true"}
    to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["signature"] = hashlib.sha1((to_sign + secret).encode()).hexdigest()
    params["api_key"] = key
    with open(path, "rb") as f:
        r = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/{rtype}/upload", data=params,
                          files={"file": (os.path.basename(path), f)}, timeout=600)
    d = r.json()
    if r.status_code >= 400 or "error" in d:
        raise ApiError(f"Cloudinary 실패 {path}: {d}")
    return d["secure_url"]


def public_url(path):
    """저장소 파일 → Meta가 가져갈 수 있는 공개 URL.
    한글 폴더명은 반드시 퍼센트 인코딩(Meta 페처는 비ASCII URL을 못 읽음).
    MEDIA_HOST=jsdelivr(기본) | raw
    """
    repo = env("GITHUB_REPOSITORY")
    sha = env("GITHUB_SHA", "main")
    rel = quote(path.replace(os.sep, "/"), safe="/")
    if env("MEDIA_HOST", "jsdelivr").lower() == "raw":
        return f"https://raw.githubusercontent.com/{repo}/{sha}/{rel}"
    return f"https://cdn.jsdelivr.net/gh/{repo}@{sha}/{rel}"


def media_urls(folder):
    base = os.path.join("posts", folder)
    slides = sorted(glob.glob(os.path.join(base, "slide_*.jpg")) + glob.glob(os.path.join(base, "slide_*.jpeg")))
    reel = next(iter(glob.glob(os.path.join(base, "reel.mp4"))), None)
    if not slides:
        raise ApiError(f"posts/{folder}/slide_*.jpg 가 없음")
    use_cld = bool(env("CLD_CLOUD_NAME") and env("CLD_API_KEY") and env("CLD_API_SECRET"))
    if use_cld:
        log("  Cloudinary 업로드")
        urls = [cld_upload(p, f"crossing_today/{folder}", f"slide_{i:02d}", "image") for i, p in enumerate(slides, 1)]
        reel_url = cld_upload(reel, f"crossing_today/{folder}", "reel", "video") if reel else None
    else:
        urls = [public_url(p) for p in slides]
        reel_url = public_url(reel) if reel else None
        log(f"  공개 URL: {urls[0]}")
        if not DRY:
            for u in urls[:1] + ([reel_url] if reel_url else []):
                r = requests.head(u, timeout=30, allow_redirects=True)
                ct = r.headers.get("content-type", "")
                if r.status_code != 200 or not (ct.startswith("image/") or ct.startswith("video/")):
                    raise ApiError(f"공개 URL 확인 실패 {u} → HTTP {r.status_code} content-type={ct}")
    return urls, reel_url


# ---------------- Notion ----------------

def nh():
    return {"Authorization": f"Bearer {env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}


def rich(p):
    return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))


def title(p):
    return "".join(t.get("plain_text", "") for t in p.get("title", []))


def fetch_due_rows():
    today = dt.datetime.now(KST).date().isoformat()
    body = {"filter": {"and": [
        {"property": "승인", "checkbox": {"equals": True}},
        {"property": "상태", "status": {"does_not_equal": "완료"}},
        {"or": [{"property": "게시 예정일", "date": {"is_empty": True}},
                {"property": "게시 예정일", "date": {"on_or_before": today}}]},
    ]}, "sorts": [{"property": "게시 예정일", "direction": "ascending"}]}
    r = requests.post(f"{NOTION}/data_sources/{env('NOTION_DATA_SOURCE_ID')}/query", headers=nh(), json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise ApiError(f"노션 조회 실패 {r.status_code}: {r.text[:400]}")
    rows = []
    for pg in r.json().get("results", []):
        P = pg["properties"]
        rows.append({"id": pg["id"], "title": title(P.get("후킹 제목", {})), "caption": rich(P.get("캡션", {})),
                     "threads_caption": rich(P.get("스레드 캡션", {})), "folder": rich(P.get("미디어 폴더", {})),
                     "already": (P.get("게시 URL", {}) or {}).get("url"),
                     "already_reel": (P.get("릴스 게시 URL", {}) or {}).get("url"),
                     "already_th": (P.get("스레드 URL", {}) or {}).get("url")})
    return rows


def update_row(page_id, props):
    if DRY:
        log(f"[DRY] 노션 갱신 {json.dumps(props, ensure_ascii=False)[:300]}")
        return
    r = requests.patch(f"{NOTION}/pages/{page_id}", headers=nh(), json={"properties": props}, timeout=TIMEOUT)
    if r.status_code >= 400:
        log(f"  노션 갱신 실패 {r.status_code}: {r.text[:300]}")


def txt(s):
    return {"rich_text": [{"text": {"content": s[:1900]}}]}


# ---------------- 메인 ----------------

def publish_row(row):
    log(f"▶ {row['title']}  (폴더 {row['folder']})")
    errors, res = [], {}
    if not row["folder"]:
        raise ApiError("'미디어 폴더' 비어 있음")
    if not row["caption"]:
        raise ApiError("'캡션' 비어 있음")
    urls, reel_url = media_urls(row["folder"])
    ig_uid, ig_tok, th_uid, th_tok = env("IG_USER_ID"), env("IG_ACCESS_TOKEN"), env("TH_USER_ID"), env("TH_ACCESS_TOKEN")

    if ig_uid and ig_tok:
        if row["already"]:
            log("  IG 캐러셀 이미 게시됨 — 건너뜀")
            res["ig"] = row["already"]
        else:
            try:
                res["ig"] = ig_carousel(ig_uid, ig_tok, urls, row["caption"]); log(f"  ✅ IG 캐러셀 {res['ig']}")
            except Exception as e:
                errors.append(f"IG 캐러셀: {e}")
        if row.get("already_reel"):
            log("  IG 릴스 이미 게시됨 — 건너뜀")
            res["reel"] = row["already_reel"]
        elif flag("POST_REELS", "true") and reel_url:
            try:
                res["reel"] = ig_reel(ig_uid, ig_tok, reel_url, row["caption"], urls[0]); log(f"  ✅ IG 릴스 {res['reel']}")
            except Exception as e:
                errors.append(f"IG 릴스: {e}")
    else:
        errors.append("IG 자격증명 없음")

    if row.get("already_th"):
        log("  Threads 이미 게시됨 — 건너뜀")
        res["th"] = row["already_th"]
    elif flag("POST_THREADS", "true"):
        if th_uid and th_tok:
            try:
                res["th"] = th_carousel(th_uid, th_tok, urls, row["threads_caption"] or row["caption"][:500]); log(f"  ✅ Threads {res['th']}")
            except Exception as e:
                errors.append(f"Threads: {e}")
        else:
            errors.append("Threads 자격증명 없음")

    stamp = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    props = {"게시 로그": txt(f"[{stamp}] " + ("성공" if not errors else "일부 실패: " + " | ".join(errors)) + (" (DRY RUN)" if DRY else ""))}
    if res.get("ig"):
        props["게시 URL"] = {"url": res["ig"]}
    if res.get("reel"):
        props["릴스 게시 URL"] = {"url": res["reel"]}
    if res.get("th"):
        props["스레드 URL"] = {"url": res["th"]}
    if res.get("ig") and not errors:
        props["상태"] = {"status": {"name": "완료"}}
    update_row(row["id"], props)
    return errors


def main():
    log(f"크로싱투데이 자동 게시 시작 {dt.datetime.now(KST):%Y-%m-%d %H:%M} KST  DRY_RUN={DRY}")
    rows = fetch_due_rows()
    if not rows:
        log("게시할 승인 행 없음. 종료.")
        return 0
    limit = int(env("MAX_POSTS_PER_RUN", "1"))
    failed = 0
    for row in rows[:limit]:
        try:
            errs = publish_row(row)
            failed += bool(errs)
        except Exception as e:
            failed += 1
            log(f"  ❌ {e}")
            update_row(row["id"], {"게시 로그": txt(f"[{dt.datetime.now(KST):%Y-%m-%d %H:%M} KST] 실패: {e}")})
    if len(rows) > limit:
        log(f"※ 승인 대기 {len(rows)-limit}건 더 있음 — 내일 이어서 게시")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
