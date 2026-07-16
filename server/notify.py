"""리뷰 종료 데스크톱 알림(macOS osascript). 대시보드를 열어두지 않아도 리뷰가 끝난
걸 알 수 있게 한다. best-effort — 알림 실패가 잡 처리를 절대 깨지 않는다."""

import subprocess

from server import config


def notify_review_done(*, repo_full: str, pr_number: int, status: str, findings: int):
    if not config.NOTIFY_ON_DONE:
        return
    label = "완료" if status == "done" else "실패"
    msg = f"{repo_full} #{pr_number} 리뷰 {label} · finding {findings}건"
    if status != "done":
        msg = f"{repo_full} #{pr_number} 리뷰 실패"
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{_esc(msg)}" with title "Almighty PR Review"',
            ],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # best-effort


def _esc(text: str) -> str:
    # AppleScript 문자열 리터럴로 들어가므로 따옴표·백슬래시만 무해화하면 된다.
    return text.replace("\\", "\\\\").replace('"', "'")
