import json
import subprocess
from dataclasses import dataclass


@dataclass
class PrInfo:
    number: int
    title: str
    author: str
    head_sha: str
    base_ref: str
    url: str
    state: str


def _default_runner(args: list[str], **kw) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


class GhClient:
    """gh CLI 얇은 래퍼. runner 주입으로 테스트 가능. write=post_comment 뿐."""

    def __init__(self, runner=_default_runner):
        self._run = runner

    def list_open_prs(self, repo: str) -> list[PrInfo]:
        out = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,title,author,headRefOid,baseRefName,url,state",
            ]
        )
        return [
            PrInfo(
                number=d["number"],
                title=d.get("title", ""),
                author=(d.get("author") or {}).get("login", ""),
                head_sha=d["headRefOid"],
                base_ref=d.get("baseRefName", ""),
                url=d.get("url", ""),
                state=d.get("state", "OPEN").lower(),
            )
            for d in json.loads(out)
        ]

    def diff(self, repo: str, number: int) -> str:
        return self._run(["gh", "pr", "diff", str(number), "--repo", repo])

    def post_comment(self, repo: str, number: int, body: str) -> dict:
        """issue comment 생성. ★개정 (codex v3 [LOW]): URL 문자열 파싱
        대신 API JSON의 .id를 그대로 저장하도록 {id, html_url}을 반환."""
        out = self._run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"/repos/{repo}/issues/{number}/comments",
                "-f",
                f"body={body}",
                "--jq",
                "{id: .id, html_url: .html_url}",
            ]
        ).strip()
        return json.loads(out)

    def edit_comment(self, repo: str, comment_id: str, body: str) -> dict:
        """★개정: 기존 issue comment를 in-place로 수정(진짜 update-or-create).
        comment_id = 숫자형 issuecomment id. {id, html_url} 반환."""
        out = self._run(
            [
                "gh",
                "api",
                "-X",
                "PATCH",
                f"/repos/{repo}/issues/comments/{comment_id}",
                "-f",
                f"body={body}",
                "--jq",
                "{id: .id, html_url: .html_url}",
            ]
        ).strip()
        return json.loads(out)
