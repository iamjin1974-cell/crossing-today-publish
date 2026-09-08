#!/usr/bin/env python3
"""
크로싱투데이 자동 게시 — GitHub Actions에서 매일 실행   (v3: 릴스에 인스타 제공 음악 자동 첨부)

흐름
  1. 노션 '크로싱투데이 콘텐츠 시트'에서  승인=체크 & 상태≠완료 & (게시 예정일 비었거나 ≤ 오늘)  행을 가져온다
  2. 행의 '미디어 폴더'(예: 27_speaking) 아래 posts/<폴더>/slide_*.jpg, reel.mp4 를 공개 URL로 만든다
       - Cloudinary 키가 있으면 Cloudinary에 올려 그 URL 사용
       - 없으면 이 저장소 파일의 jsDelivr CDN URL(기본) 또는 raw.githubusercontent.com URL 사용 (MEDIA_HOST, 저장소 public 필요)
         경로의 한글은 퍼센트 인코딩 (Meta 페처가 비ASCII URL을 못 읽음 — 27호 첫 실행에서 확인)
  3. Instagram 캐러셀 → (reel.mp4 있으면) Instagram 릴스(+음악) → Threads 캐러셀 순서로 게시
  4. 노션 행에 게시 URL·릴스 게시 URL·스레드 URL·음악·게시 로그 기록, 상태 '완료'
     (이미 URL이 있는 채널은 재실행 때 건너뜀 → 일부 실패 후 재실행해도 중복 게시 없음)

릴스 음악 (v3)
  - Meta Audio API는 'Facebook 로그인' 토큰에서만 동작. FB_PAGE_TOKEN(페이지 토큰, 만료 없음)이 있으면
    인스타 호출 전체를 graph.facebook.com 으로 보내고, 릴스 컨테이너에 audio_configuration 을 붙인다.
    FB_PAGE_TOKEN 이 없으면 예전처럼 graph.instagram.com + IG_ACCESS_TOKEN (음악 없음).
  - 곡 선택: 노션 행 '음악 키워드' → GET /ig_audio?audio_type=music&search_query=<키워드> 첫 곡.
      '음악 키워드' 비었으면 MUSIC_DEFAULT_QUERY(기본 "calm piano") 로 검색, 그것도 없으면 트렌딩 첫 곡.
      '음악 키워드'에  audio_id:1234567890  형태로 적으면 그 곡을 그대로 사용.
    ※ 실측: 검색 파라미터는 search_query (q는 무시됨), 응답 키는 "audio" (data 아님), audio_type 은 소문자 music.
  - 볼륨: MUSIC_AUDIO_VOLUME(기본 100), MUSIC_VIDEO_VOLUME(기본 0 — 영상 원본 소리는 끔)
  - 음악 첨부 실패 시(곡 없음·API 오류) 음악 없이 릴스만 올리고 로그에 남긴다 (MUSIC_REQUIRED=true 면 릴스 실패 처리)

환경변수 (GitHub Secrets)
  NOTION_TOKEN, NOTION_DATA_SOURCE_ID
  IG_USER_ID, FB_PAGE_TOKEN (권장) / IG_ACCESS_TOKEN (대체), TH_USER_ID, TH_ACCESS_TOKEN
  CLD_CLOUD_NAME, CLD_API_KEY, CLD_API_SECRET (선택)
  GITHUB_REPOSITORY (Actions가 자동 제공), DRY_RUN, POST_REELS, POST_THREADS, MAX_POSTS_PER_RUN, MEDIA_HOST(jsdelivr|raw)
  MUSIC_ENABLED(기본 true), MUSIC_DEFAULT_QUERY, MUSIC_AUDIO_VOLUME, MUSIC_VIDEO_VOLUME, MUSIC_REQUIRED
"""
import glob, hashlib, json, os, sys, time, datetime as dt
from urllib.parse import quote
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
                "permalink": "https://example.invalid/dry-run", "audio": []}
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


def ig_credentials():
    """(host, token, 음악 가능 여부). FB_PAGE_TOKEN 이 있으면 Facebook 로그인 경로."""
    fb = env("FB_PAGE_TOKEN")
    if fb:
        return IG_HOST_FBLOGIN, fb, True
    return IG_HOST_IGLOGIN, env("IG_ACCESS_TOKEN"), False


def ig_carousel(host, uid, token, urls, caption):
    if not 2 <= len(urls) <= 10:
        raise ApiError(f"인스타 캐러셀은 2~10장 (현재 {len(urls)})")
    kids = []
    for i, u in enumerate(urls, 1):
        kids.append(call("POST", f"{host}/{uid}/media", image_url=u, is_carousel_item="true", access_token=token)["id"])
        log(f"  IG 슬라이드 {i}/{len(urls)} 컨테이너 OK")
    c = call("POST", f"{host}/{uid}/media", media_type="CAROUSEL", children=",".join(kids), caption=caption, access_token=token)
    wait_ready(f"{host}/{c['id']}", token)
    p = call("POST", f"{host}/{uid}/media_publish", creation_id=c["id"], access_token=token)
    return call("GET", f"{host}/{p['id']}", fields="permalink", access_token=token).get("permalink")


# ---------------- 릴스 음악 ----------------

def search_audio(host, uid, token, query=None, limit=25):
    """Meta Audio API. query 없으면 트렌딩. 반환: [{audio_id,title,display_artist,duration_in_ms,...}]"""
    params = dict(audio_type="music", user_id=uid, limit=str(limit), access_token=token)
    if query:
        params["search_query"] = query
    d = call("GET", f"{host}/ig_audio", **params)
    return d.get("audio") or d.get("data") or []


def pick_audio(host, uid, token, keyword):
    """행의 '음악 키워드'로 곡 하나 고른다. 반환 dict 또는 None."""
    kw = (keyword or "").strip()
    if kw.lower().startswith("audio_id:"):
        aid = kw.split(":", 1)[1].strip()
        try:
            meta = call("GET", f"{host}/{aid}", user_id=uid, access_token=token)
            return {"audio_id": aid, "title": meta.get("title", ""), "display_artist": meta.get("display_artist", "")}
        except ApiError as e:
            log(f"  지정 audio_id 조회 실패({e}) — 검색으로 대체")
            kw = ""
    tries = [q for q in (kw, env("MUSIC_DEFAULT_QUERY", "calm piano")) if q] + [None]
    for q in tries:
        items = search_audio(host, uid, token, q)
        good = [a for a in items if a.get("audio_id") and int(a.get("duration_in_ms") or 0) >= 20000] or items
        if good:
            a = good[0]
            log(f"  음악 선택 ({'검색: ' + q if q else '트렌딩'}): {a.get('title')} — {a.get('display_artist')} [{a['audio_id']}]")
            return a
        log(f"  음악 검색 결과 없음 ({q})")
    return None


def audio_label(a):
    return f"{a.get('title', '')} — {a.get('display_artist', '')} (audio_id:{a.get('audio_id')})".strip()


def ig_reel(host, uid, token, video_url, caption, cover_url=None, audio=None):
    params = dict(media_type="REELS", video_url=video_url, caption=caption, share_to_feed="true", access_token=token)
    if cover_url:
        params["cover_url"] = cover_url
    if audio:
        params["audio_configuration"] = json.dumps({
            "audio_id": str(audio["audio_id"]),
            "audio_volume": int(env("MUSIC_AUDIO_VOLUME", "100")),
            "video_volume": int(env("MUSIC_VIDEO_VOLUME", "0")),
        })
    c = call("POST", f"{host}/{uid}/media", **params)
    log("  IG 릴스 컨테이너 생성" + (" (음악 첨부)" if audio else "") + ", 영상 처리 대기…")
    wait_ready(f"{host}/{c['id']}", token)
    p = call("POST", f"{host}/{uid}/media_publish", creation_id=c["id"], access_token=token)
    return call("GET", f"{host}/{p['id']}", fields="permalink", access_token=token).get("permalink")


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
                     "music_kw": rich(P.get("음악 키워드", {})),
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
        # '음악' 같은 새 열이 아직 없으면 그 속성만 빼고 한 번 더
        if "음악" in props and "is not a property" in r.text:
            props = {k: v for k, v in props.items() if k != "음악"}
            requests.patch(f"{NOTION}/pages/{page_id}", headers=nh(), json={"properties": props}, timeout=TIMEOUT)


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
    ig_host, ig_tok, music_ok = ig_credentials()
    ig_uid, th_uid, th_tok = env("IG_USER_ID"), env("TH_USER_ID"), env("TH_ACCESS_TOKEN")
    log(f"  IG 경로: {'Facebook 로그인(음악 가능)' if music_ok else 'Instagram 로그인(음악 불가)'}")

    if ig_uid and ig_tok:
        if row["already"]:
            log("  IG 캐러셀 이미 게시됨 — 건너뜀")
            res["ig"] = row["already"]
        else:
            try:
                res["ig"] = ig_carousel(ig_host, ig_uid, ig_tok, urls, row["caption"]); log(f"  ✅ IG 캐러셀 {res['ig']}")
            except Exception as e:
                errors.append(f"IG 캐러셀: {e}")
        if row.get("already_reel"):
            log("  IG 릴스 이미 게시됨 — 건너뜀")
            res["reel"] = row["already_reel"]
        elif flag("POST_REELS", "true") and reel_url:
            audio = None
            if music_ok and flag("MUSIC_ENABLED", "true"):
                try:
                    audio = pick_audio(ig_host, ig_uid, ig_tok, row.get("music_kw"))
                except Exception as e:
                    log(f"  음악 검색 오류: {e}")
                if not audio:
                    msg = "음악 없음(검색 실패) — 음악 없이 게시"
                    if flag("MUSIC_REQUIRED", "false"):
                        errors.append("IG 릴스: 음악을 찾지 못해 게시 보류")
                        audio = "SKIP"
                    else:
                        log("  " + msg); errors.append("릴스 음악: 찾지 못해 무음 게시")
            if audio != "SKIP":
                try:
                    res["reel"] = ig_reel(ig_host, ig_uid, ig_tok, reel_url, row["caption"], urls[0], audio)
                    log(f"  ✅ IG 릴스 {res['reel']}")
                    if audio:
                        res["music"] = audio_label(audio)
                except Exception as e:
                    if audio:
                        # 음악 첨부가 원인일 수 있으니 음악 없이 한 번 더
                        log(f"  음악 첨부 릴스 실패({e}) → 음악 없이 재시도")
                        try:
                            res["reel"] = ig_reel(ig_host, ig_uid, ig_tok, reel_url, row["caption"], urls[0], None)
                            log(f"  ✅ IG 릴스(무음) {res['reel']}"); errors.append(f"릴스 음악 첨부 실패: {e}")
                        except Exception as e2:
                            errors.append(f"IG 릴스: {e2}")
                    else:
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
    if res.get("music"):
        props["음악"] = txt(res["music"])
    if res.get("th"):
        props["스레드 URL"] = {"url": res["th"]}
    hard = [e for e in errors if not e.startswith("릴스 음악")]
    if res.get("ig") and not hard:
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
            failed += bool([e for e in errs if not e.startswith("릴스 음악")])
        except Exception as e:
            failed += 1
            log(f"  ❌ {e}")
            update_row(row["id"], {"게시 로그": txt(f"[{dt.datetime.now(KST):%Y-%m-%d %H:%M} KST] 실패: {e}")})
    if len(rows) > limit:
        log(f"※ 승인 대기 {len(rows)-limit}건 더 있음 — 내일 이어서 게시")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
