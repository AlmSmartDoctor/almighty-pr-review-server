import json
import subprocess
import sys
from pathlib import Path

from server.review.rollout import evaluate_scope_dedupe_rollout


def test_synthetic_review_pipeline_benchmark_is_offline_and_green():
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(root / "scripts/review-pipeline-benchmark.py")],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["external_model_invoked"] is False
    assert report["labels_exposed_to_model"] is False
    assert report["metrics"]["scope_accuracy"] == 1.0
    assert report["metrics"]["duplicate_pair_precision"] == 1.0
    assert report["metrics"]["duplicate_pair_recall"] == 1.0
    assert report["rollout"]["can_enforce"] is False
    assert "insufficient_sample" in report["rollout"]["reasons"]


def test_rollout_gate_requires_precision_recall_and_sample_size():
    green = {
        "finding_count": 100,
        "scope_accuracy": 1.0,
        "enforce_posting_accuracy": 1.0,
        "duplicate_pair_precision": 1.0,
        "duplicate_pair_recall": 0.99,
        "issue_precision": 1.0,
        "issue_recall": 0.99,
        "confidence_95_lower_bound": 0.995,
        "pr_size_strata_covered": 3,
        "partial_timeout_cases": 10,
        "cost_regression_ratio": 1.0,
    }
    assert evaluate_scope_dedupe_rollout(green).can_enforce is True

    unsafe = dict(green, duplicate_pair_precision=0.99)
    decision = evaluate_scope_dedupe_rollout(unsafe)
    assert decision.can_enforce is False
    assert decision.reasons == ("duplicate_precision",)
