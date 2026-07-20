import json
import os
import re
import subprocess
from dataclasses import dataclass

from server import config


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
    is_draft: bool = False
    base_sha: str = ""


def _default_runner(args: list[str], *, env=None, input=None) -> str:
    # env·input을 실제로 전달한다(정규화된 env 토큰 주입 + create_review의 stdin JSON
    # 페이로드 --input -). 이전엔 **kw를 받기만 하고 흘려버려 stdin이 유실됐다.
    # timeout 없으면 네트워크 행 한 번에 폴러/워커가 조용히 영구 정지한다.
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        input=input,
        timeout=config.GH_TIMEOUT_SEC,
    ).stdout


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


_native_auth: bool | None = None


def _gh_has_native_auth() -> bool:
    """gh가 env 토큰 없이도 자체 인증(keyring `gh auth login`)을 갖고 있는지.
    `gh auth token`은 keyring/config 토큰을 stdout으로 주고 로그인이 없으면 non-zero다.
    프로세스 전역 상태라 1회만 조회하고 캐시한다."""
    global _native_auth
    if _native_auth is None:
        try:
            subprocess.run(
                ["gh", "auth", "token"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            _native_auth = True
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
            OSError,
        ):
            _native_auth = False
    return _native_auth


def _normalized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if base is not None:
        env.update(base)
    if env.get("GH_TOKEN"):
        return env
    # gh는 GITHUB_TOKEN을 네이티브로 읽으므로 GH_TOKEN 승격은 precedence를 바꾸지 않는다.
    if env.get("GITHUB_TOKEN"):
        env["GH_TOKEN"] = env["GITHUB_TOKEN"]
        return env
    # GITHUB_PERSONAL_ACCESS_TOKEN은 gh가 읽지 않는 비표준 변수다. 예전엔 무조건 GH_TOKEN으로
    # 승격했는데, 이 PAT가 keyring 로그인보다 약하면(조직 private 레포에 404) 정상 인증을
    # 덮어써 프리뷰·포스팅이 깨졌다. → gh가 자체 인증을 못 가진 headless일 때만 폴백 승격한다.
    pat = env.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if pat and not _gh_has_native_auth():
        env["GH_TOKEN"] = pat
    return env


def _redact(text: str | None, env: dict[str, str]) -> str:
    out = text or ""
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        value = env.get(key)
        if value:
            out = out.replace(value, "[redacted]")
    return out


# gh는 실패 시 `(HTTP 404)`(사람용)와 JSON 본문 `"status":"404"`(API)를 남긴다.
# 실제 상태 코드를 그 형식에서 읽는다 — 예전처럼 아무 "404" 부분문자열을 집으면
# stderr에 우연히 섞인 숫자(SHA·라인번호·PR번호)를 오분류해 엉뚱한 안내를 준다.
_HTTP_RE = re.compile(r"\(HTTP (\d{3})\)")
_STATUS_RE = re.compile(r'"status"\s*:\s*"(\d{3})"')
_HTTP_LOOSE_RE = re.compile(r"\bHTTP[ /][0-9.]*\s*(\d{3})\b")


def _http_status(text: str) -> int | None:
    for rx in (_HTTP_RE, _STATUS_RE, _HTTP_LOOSE_RE):
        m = rx.search(text)
        if m:
            return int(m.group(1))
    return None


class GhClient:
    """gh CLI 얇은 래퍼. runner 주입으로 테스트 가능. write는 리뷰 게시 경로
    (create_review/update_review, 폴백 post_comment/edit_comment) 뿐."""

    def __init__(self, runner=_default_runner, env: dict[str, str] | None = None):
        self._run = runner
        self._env = env

    def _call(self, args: list[str], *, kind: str, stdin: str | None = None) -> str:
        env = _normalized_env(self._env)
        try:
            if stdin is None:
                return self._run(args, env=env)
            return self._run(args, env=env, input=stdin)
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
        except subprocess.TimeoutExpired as e:
            # "timed out"은 worker._RETRYABLE과 매칭 → 일시 장애로 재시도된다.
            raise GitHubCliError(
                exit_code=-1,
                message=f"gh {kind} timed out after {e.timeout}s",
                stderr="",
                command_kind=kind,
                http_status=None,
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
                "--limit",
                str(config.POLL_OPEN_PR_LIMIT),
                "--json",
                "number,title,author,headRefOid,baseRefName,baseRefOid,url,state,createdAt,"
                "headRefName,body,isDraft",
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
                is_draft=bool(d.get("isDraft", False)),
                base_sha=d.get("baseRefOid", ""),
            )
            for d in json.loads(out)
        ]

    def clone(self, repo: str, dest: str) -> None:
        """레포를 dest에 얕게(no-checkout, depth=1) clone. gh가 인증을 주입하므로
        private 레포도 동작. worktree가 이후 PR head ref를 추가로 fetch해 체크아웃한다.
        local_path 미설정 레포를 온디맨드로 리뷰하기 위한 소스(로컬 clone 의존 제거)."""
        self._call(
            ["gh", "repo", "clone", repo, dest, "--", "--no-checkout", "--depth=1"],
            kind="clone",
        )

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

    def create_review(
        self,
        repo: str,
        number: int,
        commit_id: str,
        body: str,
        comments: list[dict] | None = None,
    ) -> dict:
        """PR review 제출(event=COMMENT). comments=[{path,line,body}]는 diff 라인에
        붙는 인라인 코멘트(side=RIGHT). 사내 PR 봇이 pull_request_review.submitted(본문)와
        pull_request_review_comment.created(인라인)를 Slack 스레드로 중계한다.
        중첩 배열이라 -f로 못 보내므로 JSON 페이로드를 stdin(--input -)으로 전달한다.
        {id(review_id), html_url} 반환."""
        payload: dict = {"commit_id": commit_id, "event": "COMMENT", "body": body}
        if comments:
            payload["comments"] = [
                {
                    "path": c["path"],
                    "line": c["line"],
                    "side": "RIGHT",
                    "body": c["body"],
                }
                for c in comments
            ]
        out = self._call(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"/repos/{repo}/pulls/{number}/reviews",
                "--input",
                "-",
                "--jq",
                "{id: .id, html_url: .html_url}",
            ],
            kind="create_review",
            stdin=json.dumps(payload),
        ).strip()
        return json.loads(out)

    def update_review(self, repo: str, number: int, review_id: str, body: str) -> dict:
        """제출된 review의 본문(요약)만 in-place 갱신(PUT). pull_request_review.edited는
        PR 봇이 무시하므로 재게시 시 Slack 중복 알림이 없다(notify-once-on-create).
        {id, html_url} 반환."""
        out = self._call(
            [
                "gh",
                "api",
                "-X",
                "PUT",
                f"/repos/{repo}/pulls/{number}/reviews/{review_id}",
                "-f",
                f"body={body}",
                "--jq",
                "{id: .id, html_url: .html_url}",
            ],
            kind="update_review",
        ).strip()
        return json.loads(out)
