#!/usr/bin/env python3
"""
노션 '07_프로젝트 레지스트리'의 '오늘을 건너는 기록 (@crossing_today)' 행 '현재 상태' 칸 기록 (2026-11-01 Boss 지시).

- publish.py 가 게시를 시도한 실행이 끝날 때마다 report_publish() 로 '현재 상태' 칸만 덮어쓴다. 다른 칸은 건드리지 않는다.
  형식: "<YYYY-MM-DD HH:MM KST> 자동확인 · 이번 게시 <슬롯> 인스타 <성공/실패> 스레드 <성공/실패>
         · 오늘 누적 인스타 n · 스레드 n · 최근 <permalink> · 이상: <없음 또는 사유>"
  · permalink 는 이번 실행에서 API 응답으로 받은 값만. 못 받았으면 '실패'.
  · 오늘 누적은 매번 Meta API(인스타 /media, 스레드 /threads)에서 오늘(KST) 게시물을 다시 센다. 못 세면 '확인불가'.
  · 노션 쓰기 실패는 로그만 남기고 예외를 밖으로 던지지 않는다 (게시 결과에 영향 없음).
- refresh_tokens.yml 이 실패하면 `python registry.py token-fail` 로 같은 칸의 '이상'에 "토큰 갱신 실패 <날짜>"를 넣는다.
  이후 7일 동안은 publish 기록에도 그 문구를 이어 붙인다 (다음 주 갱신 전까지 사라지지 않게).

환경변수: NOTION_TOKEN (필수), REGISTRY_PAGE_ID / REGISTRY_DATA_SOURCE_ID (선택, 기본값 아래)
"""
import datetime as dt
import os
import re
import sys

import requests

NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
KST = dt.timezone(dt.timedelta(hours=9))
TIMEOUT = 30
PROP = "현재 상태"
DEFAULT_DS = "773d4b80-e883-48c8-a3b1-943b48929005"
DEFAULT_PAGE = "3f79dc9d-b848-8157-9b71-ea6fc452336d"  # '오늘을 건너는 기록 (@crossing_today) — 인스타·스레드 에세이'
ROW_KEY = "@crossing_today"
TOKEN_FAIL_RE = re.compile(r"토큰 갱신 실패 (\d{4}-\d{2}-\d{2})")
CARRY_DAYS = 7


def env(k, d=""):
    return os.environ.get(k, d).strip()


def _log(m):
    print(m, flush=True)


def _h():
    return {"Authorization": f"Bearer {env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}


class RegistryError(Exception):
    pass


def _check(r, what):
    if r.status_code < 400:
        return r.json()
    code = ""
    try:
        code = r.json().get("code", "")
    except ValueError:
        pass
    if r.status_code in (401, 403, 404) or code in ("object_not_found", "restricted_resource", "unauthorized"):
        raise RegistryError(f"연결 추가 필요 ({what} HTTP {r.status_code} {code})")
    raise RegistryError(f"{what} 실패 HTTP {r.status_code}: {r.text[:200]}")


def find_page():
    pid = env("REGISTRY_PAGE_ID", DEFAULT_PAGE)
    if pid:
        return pid
    ds = env("REGISTRY_DATA_SOURCE_ID", DEFAULT_DS)
    body = {"filter": {"property": "작업", "title": {"contains": ROW_KEY}}, "page_size": 5}
    d = _check(requests.post(f"{NOTION}/data_sources/{ds}/query", headers=_h(), json=body, timeout=TIMEOUT), "레지스트리 조회")
    res = d.get("results", [])
    if not res:
        raise RegistryError(f"레지스트리에 '{ROW_KEY}' 행 없음")
    return res[0]["id"]


def read_status(page_id):
    d = _check(requests.get(f"{NOTION}/pages/{page_id}", headers=_h(), timeout=TIMEOUT), "레지스트리 읽기")
    p = d.get("properties", {}).get(PROP, {})
    return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))


def write_status(page_id, text):
    body = {"properties": {PROP: {"rich_text": [{"text": {"content": text[:1900]}}]}}}
    _check(requests.patch(f"{NOTION}/pages/{page_id}", headers=_h(), json=body, timeout=TIMEOUT), "레지스트리 쓰기")


# ---------------- 오늘 누적 (API에서 다시 센다) ----------------

def _parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def count_today(url, params, today, max_pages=5):
    """Meta 목록 API를 최신순으로 넘기며 timestamp(KST 날짜)가 today 인 항목 수. 실패 시 예외."""
    n, page = 0, 0
    while url and page < max_pages:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        d = r.json()
        if r.status_code >= 400 or "error" in d:
            e = d.get("error", {})
            raise RuntimeError(f"{e.get('message') or d} (code {e.get('code')})")
        older = False
        for it in d.get("data", []):
            ts = it.get("timestamp")
            if not ts:
                continue
            day = _parse_ts(ts).astimezone(KST).date()
            if day == today:
                n += 1
            elif day < today:
                older = True
        if older:
            break
        url, params, page = (d.get("paging") or {}).get("next"), None, page + 1
    return n


def count_ig_today(host, uid, token, today):
    return count_today(f"{host}/{uid}/media", {"fields": "timestamp", "limit": "50", "access_token": token}, today)


def count_th_today(host, uid, token, today):
    return count_today(f"{host}/{uid}/threads", {"fields": "timestamp", "limit": "50", "access_token": token}, today)


# ---------------- 문구 ----------------

def carried_token_issue(prev, today):
    """이전 칸에 최근 7일 안의 '토큰 갱신 실패 <날짜>'가 있으면 그 문구."""
    m = None
    for m in TOKEN_FAIL_RE.finditer(prev or ""):
        pass
    if not m:
        return None
    try:
        d = dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None
    return m.group(0) if 0 <= (today - d).days < CARRY_DAYS else None


def state_word(v):
    return "해당없음" if v is None else ("성공" if v else "실패")


def build_line(now, slot, ig_ok, th_ok, ig_n, th_n, permalink, issues):
    issues = [i for i in dict.fromkeys(x.strip().replace("\n", " ") for x in issues) if i]
    return (f"{now:%Y-%m-%d %H:%M} KST 자동확인 · 이번 게시 {slot} 인스타 {state_word(ig_ok)} 스레드 {state_word(th_ok)}"
            f" · 오늘 누적 인스타 {'확인불가' if ig_n is None else ig_n} · 스레드 {'확인불가' if th_n is None else th_n}"
            f" · 최근 {permalink or '실패'} · 이상: {' / '.join(issues)[:400] if issues else '없음'}")


def with_token_issue(prev, issue):
    """'이상:' 부분에 issue 를 넣은 새 문구 (다른 부분은 그대로)."""
    prev = TOKEN_FAIL_RE.sub("", prev or "").strip()
    if " · 이상: " in prev:
        head, tail = prev.rsplit(" · 이상: ", 1)
        tail = tail.strip(" /;")
        tail = issue if tail in ("", "없음") else f"{issue} / {tail}"
        return f"{head} · 이상: {tail}"
    return (f"{prev} · 이상: {issue}" if prev else f"{dt.datetime.now(KST):%Y-%m-%d %H:%M} KST 자동확인 · 이상: {issue}")


# ---------------- 진입점 ----------------

def report_publish(slot, ig_ok, th_ok, permalink, issues, ig_count_fn=None, th_count_fn=None, log=_log):
    """게시 결과를 레지스트리에 쓴다. 반환: 써진 문구 또는 None. 절대 예외를 던지지 않는다."""
    try:
        if not env("NOTION_TOKEN"):
            log("  레지스트리: NOTION_TOKEN 없음 — 기록 건너뜀"); return None
        now = dt.datetime.now(KST)
        issues = list(issues)
        ig_n = th_n = None
        if ig_count_fn:
            try:
                ig_n = ig_count_fn(now.date())
            except Exception as e:
                issues.append(f"인스타 누적 집계 실패({str(e)[:80]})")
        if th_count_fn:
            try:
                th_n = th_count_fn(now.date())
            except Exception as e:
                issues.append(f"스레드 누적 집계 실패({str(e)[:80]})")
        pid = find_page()
        try:
            carry = carried_token_issue(read_status(pid), now.date())
        except RegistryError:
            raise
        except Exception:
            carry = None
        if carry:
            issues.append(carry)
        line = build_line(now, slot, ig_ok, th_ok, ig_n, th_n, permalink, issues)
        write_status(pid, line)
        log(f"  레지스트리 기록: {line}")
        return line
    except Exception as e:
        log(f"  레지스트리 기록 실패(게시 결과와 무관): {e}")
        return None


def report_token_failure(day=None, log=_log):
    try:
        day = day or dt.datetime.now(KST).date().isoformat()
        pid = find_page()
        line = with_token_issue(read_status(pid), f"토큰 갱신 실패 {day}")
        write_status(pid, line)
        log(f"레지스트리 기록: {line}")
        return line
    except Exception as e:
        log(f"레지스트리 기록 실패: {e}")
        return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "token-fail":
        report_token_failure()
        sys.exit(0)
    print("사용법: python registry.py token-fail")
    sys.exit(2)
