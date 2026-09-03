#!/usr/bin/env python3
"""
Instagram·Threads 장기 토큰(60일) 갱신 → GitHub Secrets에 다시 저장.  매주 1회 Actions에서 실행.
필요 Secrets: IG_ACCESS_TOKEN, TH_ACCESS_TOKEN, GH_PAT(이 저장소 Secrets 쓰기 권한)
"""
import base64, json, os, sys, requests
from nacl import encoding, public

REPO = os.environ["GITHUB_REPOSITORY"]
PAT = os.environ["GH_PAT"].strip()
H = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def set_secret(name, value):
    k = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=H, timeout=30).json()
    box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(value.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{REPO}/actions/secrets/{name}", headers=H,
                     json={"encrypted_value": enc, "key_id": k["key_id"]}, timeout=30)
    if r.status_code not in (201, 204):
        raise SystemExit(f"{name} 저장 실패 {r.status_code}: {r.text}")
    print(f"{name} 갱신 저장 완료")


def refresh(url, grant, token):
    r = requests.get(url, params={"grant_type": grant, "access_token": token}, timeout=30).json()
    if "access_token" not in r:
        raise SystemExit(f"갱신 실패: {json.dumps(r, ensure_ascii=False)}")
    print(f"  새 토큰 유효기간 {r.get('expires_in', 0)//86400}일")
    return r["access_token"]


ok = True
ig = os.environ.get("IG_ACCESS_TOKEN", "").strip()
th = os.environ.get("TH_ACCESS_TOKEN", "").strip()
if ig:
    try:
        set_secret("IG_ACCESS_TOKEN", refresh("https://graph.instagram.com/refresh_access_token", "ig_refresh_token", ig))
    except SystemExit as e:
        print(f"IG: {e}"); ok = False
if th:
    try:
        set_secret("TH_ACCESS_TOKEN", refresh("https://graph.threads.net/refresh_access_token", "th_refresh_token", th))
    except SystemExit as e:
        print(f"Threads: {e}"); ok = False
sys.exit(0 if ok else 1)
