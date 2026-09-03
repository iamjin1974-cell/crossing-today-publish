# 크로싱투데이 자동 게시 (Instagram + Threads)

노션 시트에서 **승인**을 체크한 회차를 매일 07:00 KST에 GitHub Actions가 인스타그램(캐러셀 + 릴스)과 스레드(캐러셀)에 자동으로 올리고, 게시 URL을 시트에 되돌려 적는다. 서버 없음, 비용 없음.

```
제작 창(Claude)  ──add_post.py──▶  posts/<회차>/slide_01.jpg … reel.mp4
        └── 노션 행: 캡션 · 스레드 캡션 · 미디어 폴더 · (게시 예정일)
Kim  ── 노션에서 '승인' 체크
GitHub Actions(매일 07:00) ── publish.py ──▶ Instagram 캐러셀 → 릴스 → Threads 캐러셀 → 노션에 URL 기록, 상태 '완료'
GitHub Actions(매주 월) ── refresh_tokens.py ──▶ 60일 토큰 자동 연장
```

## 1. 한 번만 하는 세팅 (Kim, 약 30분)

### A. GitHub 저장소
1. github.com → New repository → 이름 `crossing-today-publish`, **Public** (이미지를 raw URL로 Meta가 읽어야 함. Cloudinary를 쓰면 Private 가능) → Create.
2. 이 폴더의 파일을 전부 올린다 (웹에서 "Add file → Upload files"로 드래그해도 됨). `.github/workflows/` 폴더까지 포함.
3. Settings → Developer settings → **Fine-grained personal access token** 생성: Repository access = 이 저장소만, Permissions: **Contents: Read and write**, **Secrets: Read and write**. 토큰 문자열을 복사(= `GH_PAT`).

### B. 노션 통합
1. notion.so/profile/integrations → New integration → 이름 `crossing-publish`, 워크스페이스 선택 → **Internal Integration Secret** 복사(= `NOTION_TOKEN`).
2. 노션에서 **크로싱투데이 콘텐츠 시트** 페이지 열기 → 우상단 `…` → Connections → `crossing-publish` 추가.
3. `NOTION_DATA_SOURCE_ID` = `35dd6a8c-60b1-492a-85e9-e3b6a9f5af6f`

### C. Instagram (Instagram 로그인 방식 — 페이스북 페이지 불필요)
전제: @crossing_today가 **프로페셔널 계정**(크리에이터 또는 비즈니스)이어야 함. 인스타 앱 → 설정 → 계정 유형 및 도구 → 프로페셔널 계정으로 전환.
1. developers.facebook.com → My Apps → Create App → 사용 사례 **"Manage messaging and content on Instagram"** (또는 Other → Business) → 앱 생성.
2. 좌측 Instagram → **API setup with Instagram business login**.
3. 같은 화면의 "Generate access tokens" → **Add account** → @crossing_today로 로그인·권한 허용 → **Generate token** → 나오는 토큰 복사(= `IG_ACCESS_TOKEN`, 60일). 그 옆에 표시되는 Instagram account ID 복사(= `IG_USER_ID`).
   - 토큰 버튼이 안 보이면: App roles → Roles → Add People → **Instagram Tester**에 @crossing_today 추가 → 인스타 앱 설정 → 웹사이트 권한 → 앱 및 웹사이트 → 테스터 초대 수락 후 다시 시도.
4. 앱은 **개발 모드 그대로** 둔다. 본인 계정에 올리는 데는 앱 심사 불필요.

### D. Threads
1. developers.facebook.com → 새 앱 하나 더 → 사용 사례 **"Access the Threads API"** 선택.
2. Threads 사용 사례 → Settings: Redirect Callback URL / Uninstall / Delete Callback URL에 아무 https 주소(예: `https://github.com/<계정>`) 입력 후 저장.
3. App roles → Roles → Add People → **Threads Tester**에 본인 스레드 계정 추가 → 스레드 앱 → 설정 → 계정 → 웹사이트 권한 → 초대 수락.
4. Threads 사용 사례 → Settings 하단 **User Token Generator** → Generate → 토큰 복사(= `TH_ACCESS_TOKEN`, 60일). 스레드 사용자 ID는 브라우저에서
   `https://graph.threads.net/v1.0/me?fields=id,username&access_token=<토큰>` 열면 나온다(= `TH_USER_ID`).

### E. GitHub Secrets 등록
저장소 → Settings → Secrets and variables → Actions → **New repository secret**:

| 이름 | 값 |
|---|---|
| `NOTION_TOKEN` | B-1 |
| `NOTION_DATA_SOURCE_ID` | `35dd6a8c-60b1-492a-85e9-e3b6a9f5af6f` |
| `IG_USER_ID` / `IG_ACCESS_TOKEN` | C-3 |
| `TH_USER_ID` / `TH_ACCESS_TOKEN` | D-4 |
| `GH_PAT` | A-3 (토큰 자동 갱신용) |
| `CLD_CLOUD_NAME` / `CLD_API_KEY` / `CLD_API_SECRET` | 선택. Cloudinary 쓸 때만 |

같은 화면 **Variables** 탭(선택): `POST_REELS`=true/false, `POST_THREADS`=true/false, `MAX_POSTS_PER_RUN`=1.

### F. 첫 테스트
저장소 → Actions → **크로싱투데이 자동 게시** → Run workflow → dry_run = `true` → 로그에서 노션 조회·URL 생성이 정상인지 확인. 이후 승인 행 하나로 dry_run=`false` 실행 → 인스타·스레드에 실제 게시되는지 확인.

## 2. 매 회차 흐름
1. 제작 창에서 이미지·릴스 렌더 후 저장소 `posts/<회차폴더>/`에 `slide_01.jpg…`, `reel.mp4`로 업로드. Claude 클라우드 환경은 GitHub 쓰기가 막혀 있어 `add_post.py`는 **네트워크가 열린 PC에서만** 동작함(`GH_PAT=… GH_REPO=iamjin1974-cell/crossing-today-publish python3 add_post.py 27_제목 slide_*.jpg reel.mp4`). Claude 창에서는 **Claude in Chrome으로 GitHub 업로드 페이지(`…/upload/main/posts/<회차폴더>`)에 file_upload** 하거나, Kim이 PC 브라우저에서 같은 페이지에 파일을 드래그한다.
2. 노션 행: **캡션**(인스타 전체), **스레드 캡션**(500자 이내), **미디어 폴더** = `26_아침의결심`, 필요하면 **게시 예정일**.
3. Kim이 **승인** 체크 → 다음 날 07:00에 자동 게시. 하루 1회차(MAX_POSTS_PER_RUN)만 나가고 나머지는 다음 날 이어서.
4. 결과는 행의 **게시 URL / 릴스 게시 URL / 스레드 URL / 게시 로그**, 상태 **완료**. 실패하면 게시 로그에 원인이 남고 상태는 그대로(다음 날 재시도).

## 3. 알아둘 것
- 인스타 이미지: JPEG, 1080×1350(4:5) OK, 8MB 이하, 2~10장. 릴스: 9:16 mp4, 3초~15분.
- 스레드 본문 500자 제한. 캐러셀 2~20장.
- 토큰은 60일 유효 → 매주 월요일 자동 연장. 연장이 실패하면(Actions 이메일 알림) C-3 / D-4를 다시 해서 Secrets를 덮어쓴다.
- API 게시 한도: 인스타 100건/24h, 스레드 250건/24h — 하루 1~2회차엔 전혀 문제 없음.
- 게시 URL이 이미 있는 행은 인스타 캐러셀을 다시 올리지 않는다(중복 방지).
