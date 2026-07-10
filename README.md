# Almighty PR Review Server

로컬 단일사용자 멀티벤더(Claude+Codex) PR 리뷰 서버.

## 실행
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m server.main          # http://127.0.0.1:8787
cd web && npm install && npm run dev   # http://localhost:5173
```

## 사전 요구
- `gh` 로그인(`gh auth status`), `claude`/`codex` CLI 로그인.
- 리뷰 대상 레포는 로컬에 clone 되어 있어야 함(격리 worktree 소스).

## 안전
- 리뷰 워커: read-only 툴만. 유일 write = 승인 후 PR 코멘트.
- 전역 프로파일 미상속(리뷰 전용 하네스). 격리 worktree.

## 아키텍처 / 데이터모델
`docs/superpowers/specs/2026-07-07-almighty-pr-review-design.md` 참조.

## E2E 스모크
```bash
ALMIGHTY_E2E=1 ALMIGHTY_E2E_REPO=me/sandbox \
  ALMIGHTY_E2E_LOCAL=/path/to/local/clone \
  pytest tests/test_e2e_smoke.py -v
```
특정 PR만 smoke하려면 작은 PR 기준으로 `ALMIGHTY_E2E_PR=2414`처럼 추가한다.
