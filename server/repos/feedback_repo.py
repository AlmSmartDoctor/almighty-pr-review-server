"""서브프로젝트 C — Slack 반응 학습 신호의 write-side(seams.NullMemoryStore를 구체화).
코드베이스 컨벤션대로 클래스가 아닌 함수 모듈. 두 테이블을 다룬다:
  slack_post      — 리뷰를 게시한 Slack 메시지(run ↔ channel:ts) 매핑
  feedback_signal — 그 메시지에 달린 👍/👎 현재 상태(added=INSERT, removed=DELETE)
"""


def record_slack_post(conn, *, run_id, channel, ts) -> int:
    """리뷰를 게시한 Slack 메시지를 run에 매핑한다(반응 역참조 키). 같은 (channel, ts)면 무시."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO slack_post (run_id, channel, ts, posted_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (run_id, channel, ts),
    )
    conn.commit()
    return cur.lastrowid


def has_slack_post(conn, run_id) -> bool:
    """이 run을 이미 Slack에 게시했는지(멱등 게시 게이트)."""
    return (
        conn.execute(
            "SELECT 1 FROM slack_post WHERE run_id=? LIMIT 1", (run_id,)
        ).fetchone()
        is not None
    )


def run_for_message(conn, *, channel, ts):
    """반응이 달린 Slack 메시지의 run_id를 역참조한다(우리가 게시한 메시지가 아니면 None)."""
    row = conn.execute(
        "SELECT run_id FROM slack_post WHERE channel=? AND ts=?", (channel, ts)
    ).fetchone()
    return row["run_id"] if row is not None else None


def add_reaction(conn, *, run_id, slack_user, reaction, verdict) -> None:
    """reaction_added — 현재 상태에 반응 1건 기록(같은 사람·이모지 중복은 무시)."""
    conn.execute(
        """INSERT OR IGNORE INTO feedback_signal
           (run_id, source, slack_user, reaction, verdict, created_at)
           VALUES (?, 'slack', ?, ?, ?, datetime('now'))""",
        (run_id, slack_user, reaction, verdict),
    )
    conn.commit()


def remove_reaction(conn, *, run_id, slack_user, reaction) -> None:
    """reaction_removed — 해당 반응 상태 제거(현재-상태 모델)."""
    conn.execute(
        """DELETE FROM feedback_signal
           WHERE run_id=? AND source='slack' AND slack_user=? AND reaction=?""",
        (run_id, slack_user, reaction),
    )
    conn.commit()
