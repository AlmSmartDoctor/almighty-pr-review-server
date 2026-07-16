from server import config
from server.context.base import read_confined


def file_project_source(*, path: str, root: str):
    """레포에 체크인된 프로젝트 문서(진행상황·아키텍처·도메인 개요) 전체를 주입하는
    graph_source(req)->str 콜백을 만든다. 변경 파일과 무관하게 항상 문서 전체를 준다
    (애그리게이터 1차 증분). 미지정/경계밖/미도달/오류는 ""로 degrade.
    per-source 캡은 downstream(render_context)이 처리 — 여기선 상한 바이트만 읽는다.
    향후 증분(DB 특징·서버 데이터)은 같은 graph_source seam에 스택된다."""

    def source(req) -> str:
        root_eff = (
            getattr(req, "workdir", "") or root
        )  # PR-head worktree 우선, local_path 폴백
        return (
            read_confined(path, root_eff, config.MAX_CONTEXT_CHARS_PER_SOURCE + 1) or ""
        )

    return source
