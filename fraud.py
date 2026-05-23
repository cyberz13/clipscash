"""Fraud-scoring heuristic for submissions.

Score 0–100:
  Safe        <  30
  Suspicious  30 – 60
  Reject      >= 61
"""
from __future__ import annotations
from typing import Optional


def compute_fraud_score(
    self_views: int,
    self_likes: int,
    self_comments: int,
    verified_views: Optional[int] = None,
    verified_likes: Optional[int] = None,
    proof_count: int = 0,
    creator_age_days: int = 0,
    prior_approved: int = 0,
    prior_rejected: int = 0,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if proof_count == 0:
        score += 25
        reasons.append("No screenshots attached")
    elif proof_count == 1:
        score += 8
        reasons.append("Only one screenshot")

    if self_views <= 0:
        score += 10
        reasons.append("Zero self-reported views")
    elif self_views > 5_000_000:
        score += 15
        reasons.append("Unrealistic view count")

    engagement_rate = (self_likes + self_comments) / max(self_views, 1)
    if engagement_rate > 0.5:
        score += 20
        reasons.append("Engagement rate above 50% (very unusual)")
    elif engagement_rate < 0.002 and self_views > 1000:
        score += 10
        reasons.append("Engagement rate below 0.2% (low-quality traffic)")

    if self_likes > self_views:
        score += 25
        reasons.append("Likes exceed views")

    if verified_views is not None and self_views > 0:
        ratio = verified_views / self_views
        if ratio < 0.3:
            score += 30
            reasons.append("Verified views < 30% of self-reported")
        elif ratio < 0.6:
            score += 12
            reasons.append("Verified views significantly lower than reported")

    if verified_likes is not None and self_likes > 0:
        if verified_likes / max(self_likes, 1) < 0.4:
            score += 8
            reasons.append("Verified likes much lower than reported")

    if creator_age_days < 3:
        score += 10
        reasons.append("New account (< 3 days)")

    total_prior = prior_approved + prior_rejected
    if total_prior >= 5:
        rej_rate = prior_rejected / total_prior
        if rej_rate >= 0.6:
            score += 15
            reasons.append("High past rejection rate")
        elif prior_approved >= 10 and rej_rate <= 0.1:
            score = max(0, score - 10)
            reasons.append("Strong approval history (−10)")

    return max(0, min(100, score)), reasons


def fraud_band(score: int) -> str:
    if score < 30:
        return "safe"
    if score < 61:
        return "suspicious"
    return "reject"
