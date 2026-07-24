from dataclasses import dataclass


@dataclass(frozen=True)
class RolloutDecision:
    can_enforce: bool
    reasons: tuple[str, ...]


def _report_gate(metrics: dict, *, min_findings: int, min_pr_size_strata: int,
                 min_partial_timeout_cases: int, min_hard_negative_pairs: int,
                 max_cost_regression_ratio: float) -> RolloutDecision:
    """Gate a Task 3.2 aggregate report using its unrounded metric evidence."""
    report_metrics = metrics.get("metrics", {})
    evidence = metrics.get("rollout_evidence", {})
    reasons: list[str] = []
    if int(metrics.get("finding_count", 0)) < min_findings:
        reasons.append("insufficient_sample")
    # The scorer has already applied the contract-specific point/LB/minimum-N gates;
    # do not use a generic confidence lower bound for a different metric.
    for report_name, reason in (
        ("scope_accuracy", "scope_accuracy"), ("posting_accuracy", "posting_accuracy"),
        ("duplicate_precision", "duplicate_precision"), ("duplicate_recall", "duplicate_recall"),
        ("issue_precision", "issue_precision"), ("issue_recall", "issue_recall"),
    ):
        value = report_metrics.get(report_name)
        if not isinstance(value, dict) or not value.get("passed", False):
            reasons.append(reason)
    cost = report_metrics.get("cost_regression")
    if not isinstance(cost, dict) or not cost.get("passed", False) or float(cost.get("point_estimate", float("inf"))) > max_cost_regression_ratio:
        reasons.append("cost_regression")
    if int(evidence.get("pr_size_strata_covered", 0)) < min_pr_size_strata:
        reasons.append("pr_size_strata")
    if int(evidence.get("partial_timeout_cases", 0)) < min_partial_timeout_cases:
        reasons.append("partial_timeout_coverage")
    if int(evidence.get("hard_negative_pairs", 0)) < min_hard_negative_pairs:
        reasons.append("hard_negative_sample")
    if not metrics.get("can_enforce", False):
        for reason in metrics.get("failure_reasons", []):
            if reason in {"cost_locked", "not_invoked_setup_failure", "quality_locked"}:
                reasons.append(reason)
    return RolloutDecision(not reasons, tuple(dict.fromkeys(reasons)))


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
    min_hard_negative_pairs: int = 30,
    max_cost_regression_ratio: float = 1.10,
) -> RolloutDecision:
    """Apply conservative benchmark gates before repository canary enablement.

    Benchmark-report inputs use the scorer's explicit numerator/denominator and
    contract-specific Wilson lower bounds.  The legacy flat mapping remains solely
    for the pre-Task-3.2 synthetic benchmark test.
    """
    if "metrics" in metrics:
        return _report_gate(
            metrics, min_findings=min_findings,
            min_pr_size_strata=min_pr_size_strata,
            min_partial_timeout_cases=min_partial_timeout_cases,
            min_hard_negative_pairs=min_hard_negative_pairs,
            max_cost_regression_ratio=max_cost_regression_ratio,
        )
    reasons = []
    checks = (
        (int(metrics.get("finding_count", 0)) >= min_findings, "insufficient_sample"),
        (float(metrics.get("scope_accuracy", 0)) >= min_scope_accuracy, "scope_accuracy"),
        (float(metrics.get("enforce_posting_accuracy", 0)) >= min_posting_accuracy, "posting_accuracy"),
        (float(metrics.get("duplicate_pair_precision", 0)) >= min_duplicate_precision, "duplicate_precision"),
        (float(metrics.get("duplicate_pair_recall", 0)) >= min_duplicate_recall, "duplicate_recall"),
        (float(metrics.get("issue_precision", 0)) >= min_issue_precision, "issue_precision"),
        (float(metrics.get("issue_recall", 0)) >= min_issue_recall, "issue_recall"),
        (float(metrics.get("confidence_95_lower_bound", 0)) >= min_confidence_lower_bound, "confidence_interval"),
        (int(metrics.get("pr_size_strata_covered", 0)) >= min_pr_size_strata, "pr_size_strata"),
        (int(metrics.get("partial_timeout_cases", 0)) >= min_partial_timeout_cases, "partial_timeout_coverage"),
        (float(metrics.get("cost_regression_ratio", float("inf"))) <= max_cost_regression_ratio, "cost_regression"),
    )
    for passed, reason in checks:
        if not passed:
            reasons.append(reason)
    return RolloutDecision(not reasons, tuple(reasons))
