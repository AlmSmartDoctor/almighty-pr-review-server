import json
import os
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
    created_at: str | None = None
    head_ref: str = ""
    body: str = ""


def _default_runner(args: list[str], **kw) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


class GitHubCliError(RuntimeError):
    def __init__(
        self,
        *,
        exit_code: int,
        message: str,
        stderr: str,
        command_kind: str,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.stderr = stderr
        self.command_kind = command_kind
        self.http_status = http_status


def _normalized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if base is not None:
        env.update(base)
    if not env.get("GH_TOKEN"):
        for key in ("GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
            if env.get(key):
                env["GH_TOKEN"] = env[key]
                break
    return env


def _redact(text: str | None, env: dict[str, str]) -> str:
    out = text or ""
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        value = env.get(key)
        if value:
            out = out.replace(value, "[redacted]")
    return out


def _http_status(text: str) -> int | None:
    for code in (401, 403, 404):
        if str(code) in text:
            return code
    return None


class GhClient:
    """gh CLI 얇은 래퍼. runner 주입으로 테스트 가능. write=post_comment 뿐."""

    def __init__(self, runner=_default_runner, env: dict[str, str] | None = None):
        self._run = runner
        self._env = env

    def _call(self, args: list[str], *, kind: str) -> str:
        env = _normalized_env(self._env)
        try:
            return self._run(args, env=env)
        except subprocess.CalledProcessError as e:
            stderr = _redact(e.stderr, env)
            stdout = _redact(
                getattr(e, "stdout", None) or getattr(e, "output", None), env
            )
            status = _http_status(f"{stderr}\n{stdout}")
            message = stderr.strip() or stdout.strip() or f"gh {kind} failed"
            raise GitHubCliError(
                exit_code=e.returncode,
                message=message,
                stderr=stderr,
                command_kind=kind,
                http_status=status,
            ) from e

    def list_open_prs(self, repo: str) -> list[PrInfo]:
        out = self._call(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,title,author,headRefOid,baseRefName,url,state,createdAt,"
                "headRefName,body",
            ],
            kind="list_open_prs",
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
                created_at=d.get("createdAt"),
                head_ref=d.get("headRefName", ""),
                body=d.get("body", ""),
            )
            for d in json.loads(out)
        ]

    def diff(self, repo: str, number: int) -> str:
        return self._call(
            ["gh", "pr", "diff", str(number), "--repo", repo], kind="diff"
        )

    def compare_diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        """base...head(three-dot: merge-base→head) 구간의 통합 diff.
        증분 리뷰용 — 직전 완료 리뷰 이후 추가된 변경만 얻는다. force-push로 base가
        조상이 아니면 merge-base 기준이라 과대 포함(누락 없음, 안전)."""
        return self._call(
            [
                "gh",
                "api",
                f"/repos/{repo}/compare/{base_sha}...{head_sha}",
                "-H",
                "Accept: application/vnd.github.diff",
            ],
            kind="compare_diff",
        )

    def preflight_user(self) -> dict:
        out = self._call(
            ["gh", "api", "user", "--jq", "{login: .login}"],
            kind="preflight_user",
        ).strip()
        return json.loads(out)

    def preflight_repo(self, repo: str) -> dict:
        out = self._call(
            ["gh", "api", f"/repos/{repo}", "--jq", "{full_name: .full_name}"],
            kind="preflight_repo",
        ).strip()
        return json.loads(out)

    def preflight_issue(self, repo: str, number: int) -> dict:
        out = self._call(
            [
                "gh",
                "api",
                f"/repos/{repo}/issues/{number}",
                "--jq",
                "{number: .number}",
            ],
            kind="preflight_issue",
        ).strip()
        return json.loads(out)

    def post_comment(self, repo: str, number: int, body: str) -> dict:
        """issue comment 생성. ★개정 (codex v3 [LOW]): URL 문자열 파싱
        대신 API JSON의 .id를 그대로 저장하도록 {id, html_url}을 반환."""
        out = self._call(
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
            ],
            kind="post_comment",
        ).strip()
        return json.loads(out)

    def edit_comment(self, repo: str, comment_id: str, body: str) -> dict:
        """★개정: 기존 issue comment를 in-place로 수정(진짜 update-or-create).
        comment_id = 숫자형 issuecomment id. {id, html_url} 반환."""
        out = self._call(
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
            ],
            kind="edit_comment",
        ).strip()
        return json.loads(out)
