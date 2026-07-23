from dataclasses import dataclass


@dataclass(frozen=True)
class RolloutDecision:
    can_enforce: bool
    reasons: tuple[str, ...]


def evaluate_scope_dedupe_rollout(
    metrics: dict,
    *,
    min_findings: int = 100,
    min_scope_accuracy: float = 0.995,
    min_posting_accuracy: float = 0.995,
    min_duplicate_precision: float = 1.0,
    min_duplicate_recall: float = 0.95,
    min_issue_precision: float = 0.995,
    min_issue_recall: float = 0.95,
    min_confidence_lower_bound: float = 0.99,
    min_pr_size_strata: int = 3,
    min_partial_timeout_cases: int = 10,
    max_cost_regression_ratio: float = 1.10,
) -> RolloutDecision:
    """Apply conservative, explicit gates before a repository canary is enabled."""
    reasons = []
    checks = (
        (int(metrics.get("finding_count", 0)) >= min_findings, "insufficient_sample"),
        (float(metrics.get("scope_accuracy", 0)) >= min_scope_accuracy, "scope_accuracy"),
        (
            float(metrics.get("enforce_posting_accuracy", 0)) >= min_posting_accuracy,
            "posting_accuracy",
        ),
        (
            float(metrics.get("duplicate_pair_precision", 0))
            >= min_duplicate_precision,
            "duplicate_precision",
        ),
        (
            float(metrics.get("duplicate_pair_recall", 0)) >= min_duplicate_recall,
            "duplicate_recall",
        ),
        (
            float(metrics.get("issue_precision", 0)) >= min_issue_precision,
            "issue_precision",
        ),
        (
            float(metrics.get("issue_recall", 0)) >= min_issue_recall,
            "issue_recall",
        ),
        (
            float(metrics.get("confidence_95_lower_bound", 0))
            >= min_confidence_lower_bound,
            "confidence_interval",
        ),
        (
            int(metrics.get("pr_size_strata_covered", 0)) >= min_pr_size_strata,
            "pr_size_strata",
        ),
        (
            int(metrics.get("partial_timeout_cases", 0)) >= min_partial_timeout_cases,
            "partial_timeout_coverage",
        ),
        (
            float(metrics.get("cost_regression_ratio", float("inf")))
            <= max_cost_regression_ratio,
            "cost_regression",
        ),
    )
    for passed, reason in checks:
        if not passed:
            reasons.append(reason)
    return RolloutDecision(not reasons, tuple(reasons))
