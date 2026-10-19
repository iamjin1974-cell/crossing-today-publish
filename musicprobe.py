#!/usr/bin/env python3
"""인스타 음원 라이브러리에 Kim 곡이 들어가 있는지 확인 (게시 없음).
결과를 노션 행(PROBE_PAGE)의 '음악' 칸에 적는다. GitHub 로그를 못 읽는 환경용.
환경변수: PROBE_QUERIES (쉼표 구분), PROBE_PAGE (노션 페이지 ID)"""
import publish as P

def main():
    host, tok, ok = P.ig_credentials()
    uid = P.env("IG_USER_ID")
    lines = []
    for q in [x.strip() for x in P.env("PROBE_QUERIES", "심야 역전다방").split(",") if x.strip()]:
        try:
            items = P.search_audio(host, uid, tok, q, limit=50)
            mine = P.artist_tracks(host, uid, tok, q)
            lines.append(f"[{q}] 검색 {len(items)}건 / 아티스트 일치 {len(mine)}곡: " +
                         ", ".join(f"{a.get('title')}({a['audio_id']})" for a in mine[:20]))
            if not mine:
                lines.append("   상위 결과: " + ", ".join(f"{a.get('title')}—{a.get('display_artist')}" for a in items[:5]))
        except Exception as e:
            lines.append(f"[{q}] 오류: {e}")
    out = "\n".join(lines)
    P.log(out)
    page = P.env("PROBE_PAGE")
    if page:
        P.update_row(page, {"음악": P.txt("[음원 확인] " + out[:1900])})

if __name__ == "__main__":
    main()
