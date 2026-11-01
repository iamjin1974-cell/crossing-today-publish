#!/usr/bin/env python3
"""
Threads(·Instagram) 장기 토큰(60일) 갱신 → GitHub Secrets에 다시 저장. 매주 1회 Actions에서 실행.
- Threads를 먼저 처리한다 (실제로 쓰는 토큰).
- IG는 FB_PAGE_TOKEN(만료 없음)으로 게시하므로 기본 건너뜀. 환경변수 IG_REFRESH=1일 때만 갱신.
- 어떤 오류든 원인(HTTP 코드·응답)을 로그에 남기고 다음 토큰으로 넘어간다.
필요 Secrets: TH_ACCESS_TOKEN, GH_PAT(이 저장소 Secrets 읽기·쓰기 권한)
"""
import base64, json, os, sys, requests
from nacl import encoding, public

REPO = os.environ["GITHUB_REPOSITORY"]
PAT = os.environ.get("GH_PAT", "").strip()
H = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def check_pat():
    if not PAT:
        raise RuntimeError("GH_PAT 비어 있음 — 저장소 Secrets에 GH_PAT 등록 필요")
    r = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=H, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Secrets 공개키 조회 실패 HTTP {r.status_code}: {r.text[:300]}")
    k = r.json()
    if "key" not in k or "key_id" not in k:
        raise RuntimeError(f"공개키 응답 이상: {json.dumps(k, ensure_ascii=False)[:300]}")
    return k


def set_secret(name, value, k):
    box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(value.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{REPO}/actions/secrets/{name}", headers=H,
                     json={"encrypted_value": enc, "key_id": k["key_id"]}, timeout=30)
    if r.status_code not in (201, 204):
        raise RuntimeError(f"{name} 저장 실패 HTTP {r.status_code}: {r.text[:300]}")
    print(f"  {name} 갱신 저장 완료")


def refresh(url, grant, token):
    r = requests.get(url, params={"grant_type": grant, "access_token": token}, timeout=30).json()
    if "access_token" not in r:
        raise RuntimeError(f"Meta 갱신 실패: {json.dumps(r, ensure_ascii=False)[:300]}")
    print(f"  새 토큰 유효기간 {r.get('expires_in', 0)//86400}일")
    return r["access_token"]


ok = True
try:
    key = check_pat()
    print("GH_PAT 확인 OK (Secrets 공개키 조회 성공)")
except Exception as e:
    print(f"GH_PAT 문제: {e}")
    sys.exit(1)

th = os.environ.get("TH_ACCESS_TOKEN", "").strip()
if th:
    print("Threads 갱신:")
    try:
        set_secret("TH_ACCESS_TOKEN", refresh("https://graph.threads.net/refresh_access_token", "th_refresh_token", th), key)
    except Exception as e:
        print(f"  Threads 실패: {e}"); ok = False
else:
    print("Threads: TH_ACCESS_TOKEN 없음 — 건너뜀"); ok = False

ig = os.environ.get("IG_ACCESS_TOKEN", "").strip()
if ig and os.environ.get("IG_REFRESH", "") == "1":
    print("Instagram 갱신:")
    try:
        set_secret("IG_ACCESS_TOKEN", refresh("https://graph.instagram.com/refresh_access_token", "ig_refresh_token", ig), key)
    except Exception as e:
        print(f"  IG 실패: {e}"); ok = False
else:
    print("Instagram: FB_PAGE_TOKEN(만료 없음) 사용 중 — 갱신 건너뜀")

sys.exit(0 if ok else 1)
