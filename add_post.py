#!/usr/bin/env python3
"""
제작 창에서 렌더한 JPG(+reel.mp4)를 이 저장소 posts/<폴더>/ 에 올린다 (git clone 불필요, GitHub API 사용).

  GH_PAT=… GH_REPO=<계정>/<저장소> python3 add_post.py 26_아침의결심 slide_01.jpg slide_02.jpg ... [reel.mp4]

파일명은 올리면서 slide_01.jpg, slide_02.jpg … / reel.mp4 로 자동 정리된다. 인스타는 JPEG만 받으므로 PNG는 JPG로 변환한다.
끝나면 노션 시트 행 '미디어 폴더'에 폴더명을 적는다.
"""
import base64, mimetypes, os, sys, requests

PAT, REPO = os.environ["GH_PAT"].strip(), os.environ["GH_REPO"].strip()
H = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json"}


def put(path_in_repo, local):
    url = f"https://api.github.com/repos/{REPO}/contents/{path_in_repo}"
    sha = requests.get(url, headers=H, timeout=30).json().get("sha")
    body = {"message": f"add {path_in_repo}", "content": base64.b64encode(open(local, "rb").read()).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=H, json=body, timeout=300)
    if r.status_code not in (200, 201):
        raise SystemExit(f"업로드 실패 {path_in_repo}: {r.status_code} {r.text[:200]}")
    print(f"OK  {path_in_repo}")


def ensure_jpeg(p):
    if p.lower().endswith((".jpg", ".jpeg")):
        return p
    from PIL import Image
    out = os.path.splitext(p)[0] + ".jpg"
    Image.open(p).convert("RGB").save(out, "JPEG", quality=92)
    return out


folder, files = sys.argv[1], sys.argv[2:]
n = 0
for f in files:
    if (mimetypes.guess_type(f)[0] or "").startswith("video"):
        put(f"posts/{folder}/reel.mp4", f)
    else:
        n += 1
        put(f"posts/{folder}/slide_{n:02d}.jpg", ensure_jpeg(f))
print(f"\n완료: posts/{folder}/  슬라이드 {n}장" + ("" if n else " ⚠ 슬라이드 없음"))
