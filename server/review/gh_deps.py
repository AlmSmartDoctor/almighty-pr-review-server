def build_deps(repo):
    """Task 7.2에서 실제 PipelineDeps(gh/worktree/prescreen/adapters) 조립으로
    대체되는 v1 스텁. worker_loop은 Milestone 7 lifespan에서만 기동되며 그 전에
    7.2가 이 함수를 구현하므로, 프로덕션 경로에서 None이 review_pr로 흘러가지 않는다.
    (worker 테스트는 review_pr를 monkeypatch해 deps를 사용하지 않는다.)"""
    return None
