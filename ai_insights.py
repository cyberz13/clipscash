"""Deterministic 'AI' insights for campaigns and creator matching.

Produces health scores, strengths/weaknesses, recommendations, and creator
matches based on real data — no external API required. Swappable later with
a real LLM call.
"""
from __future__ import annotations
from typing import Any


def analyze_campaign(campaign: dict, submissions: list[dict]) -> dict:
    """Return a structured campaign analysis."""
    total_subs = len(submissions)
    approved = [s for s in submissions if s["status"] in ("approved", "paid")]
    rejected = [s for s in submissions if s["status"] == "rejected"]
    pending = [s for s in submissions if s["status"] == "pending"]

    approval_rate = len(approved) / total_subs if total_subs else 0
    total_views = sum(s.get("verified_views", 0) or s.get("self_views", 0) for s in approved)
    total_spend = campaign["spent_cents"]
    budget = max(campaign["budget_cents"], 1)
    budget_used_pct = total_spend / budget

    cpm = (total_spend / 100.0) / (total_views / 1000.0) if total_views else 0

    score = 50
    strengths: list[str] = []
    weaknesses: list[str] = []
    recs: list[str] = []

    if total_subs == 0:
        score = 35
        weaknesses.append("no_submissions_yet")
        recs.append("rec_boost_visibility")
    else:
        if approval_rate >= 0.6:
            score += 15
            strengths.append("high_approval_rate")
        elif approval_rate < 0.3:
            score -= 10
            weaknesses.append("low_approval_rate")
            recs.append("rec_clarify_brief")

        if total_views >= 100_000:
            score += 15
            strengths.append("strong_reach")
        elif total_views < 10_000 and len(approved) >= 3:
            weaknesses.append("low_reach_per_post")
            recs.append("rec_higher_per_view_rate")

        if budget_used_pct > 0.9:
            weaknesses.append("budget_nearly_exhausted")
            recs.append("rec_topup_budget")
        elif budget_used_pct < 0.2 and total_subs >= 5:
            score -= 5
            recs.append("rec_increase_payout")

        if len(pending) > 10:
            weaknesses.append("backlog_in_review")
            recs.append("rec_review_faster")

        if campaign["payout_type"] == "per_view" and cpm > 6:
            weaknesses.append("high_cpm")
            recs.append("rec_lower_per_view")
        if campaign["payout_type"] == "per_view" and 0 < cpm < 1.5:
            strengths.append("excellent_cpm")

    score = max(0, min(100, score))
    return {
        "score": score,
        "band": "great" if score >= 75 else "ok" if score >= 50 else "weak",
        "stats": {
            "submissions": total_subs,
            "approved": len(approved),
            "rejected": len(rejected),
            "pending": len(pending),
            "approval_rate": round(approval_rate * 100, 1),
            "total_views": total_views,
            "spend_cents": total_spend,
            "budget_used_pct": round(budget_used_pct * 100, 1),
            "cpm": round(cpm, 2),
        },
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recs,
    }


# Insight text dictionary (bilingual)
INSIGHT_TEXT = {
    "ar": {
        "no_submissions_yet": "لم تصل أي تقديمات بعد.",
        "high_approval_rate": "معدل موافقة مرتفع — جودة المبدعين ممتازة.",
        "low_approval_rate": "نسبة الرفض مرتفعة — قد يكون البريف غير واضح.",
        "strong_reach": "وصول قوي عبر المنصات.",
        "low_reach_per_post": "متوسط المشاهدات للمنشور منخفض.",
        "budget_nearly_exhausted": "الميزانية شارفت على الانتهاء.",
        "backlog_in_review": "تراكم في تقديمات بانتظار المراجعة.",
        "high_cpm": "تكلفة الألف مشاهدة (CPM) مرتفعة مقارنة بالسوق.",
        "excellent_cpm": "CPM ممتازة مقارنة بمتوسط السوق.",
        "rec_boost_visibility": "روّج للحملة على الصفحة الرئيسية أو ارفع المكافأة.",
        "rec_clarify_brief": "أعد صياغة البريف بأمثلة فيديو واضحة.",
        "rec_higher_per_view_rate": "ارفع سعر الألف مشاهدة لجذب مبدعين أكبر.",
        "rec_topup_budget": "أضف رصيداً قبل توقف الحملة.",
        "rec_increase_payout": "حملتك تحت الاستخدام — زد المكافأة لتسريع التقديمات.",
        "rec_review_faster": "راجع التقديمات المعلقة لتفادي إحباط المبدعين.",
        "rec_lower_per_view": "خفّض سعر الألف مشاهدة قليلاً لتحسين العائد.",
    },
    "en": {
        "no_submissions_yet": "No submissions have come in yet.",
        "high_approval_rate": "Strong approval rate — creator quality is high.",
        "low_approval_rate": "Rejection rate is high — the brief may be unclear.",
        "strong_reach": "Strong reach across platforms.",
        "low_reach_per_post": "Average views per post are low.",
        "budget_nearly_exhausted": "Budget is nearly used up.",
        "backlog_in_review": "Submissions are piling up in review.",
        "high_cpm": "CPM is above the market average.",
        "excellent_cpm": "CPM is significantly below market average.",
        "rec_boost_visibility": "Feature the campaign or raise the payout.",
        "rec_clarify_brief": "Rewrite the brief with concrete video examples.",
        "rec_higher_per_view_rate": "Raise the per-1k-views rate to attract larger creators.",
        "rec_topup_budget": "Top up the budget before the campaign stalls.",
        "rec_increase_payout": "Your campaign is under-utilised — raise the payout.",
        "rec_review_faster": "Clear the review backlog to avoid frustrating creators.",
        "rec_lower_per_view": "Lower the per-1k rate slightly to improve return.",
    },
}


def insight_text(key: str, lang: str = "ar") -> str:
    return INSIGHT_TEXT.get(lang, INSIGHT_TEXT["ar"]).get(key, key)


def suggest_creators(campaign: dict, all_creators: list[dict], limit: int = 10) -> list[dict]:
    """Rank creators for a given campaign using prior performance + platform fit."""
    wanted_platforms = set(p.strip() for p in (campaign["platforms"] or "").split(",") if p.strip())
    scored: list[tuple[int, dict, list[str]]] = []

    for c in all_creators:
        score = 0
        reasons: list[str] = []
        total = (c.get("prior_approved", 0) or 0) + (c.get("prior_rejected", 0) or 0)
        approval = (c.get("prior_approved", 0) / total) if total else 0
        avg_views = c.get("avg_views", 0) or 0

        if total >= 3 and approval >= 0.7:
            score += 35
            reasons.append(f"{int(approval*100)}% approval over {total} posts")
        elif total >= 1 and approval >= 0.5:
            score += 15
            reasons.append("solid track record")

        if avg_views >= 50_000:
            score += 25
            reasons.append(f"avg {avg_views:,} views/post")
        elif avg_views >= 10_000:
            score += 12
            reasons.append(f"avg {avg_views:,} views/post")

        socials = (c.get("socials") or "").lower()
        platform_hits = sum(1 for p in wanted_platforms if p in socials)
        if platform_hits:
            score += 10 * platform_hits
            reasons.append(f"active on {platform_hits} target platform(s)")

        avg_fraud = c.get("avg_fraud", 0) or 0
        if avg_fraud >= 50:
            score -= 30
            reasons.append("elevated fraud risk")
        elif avg_fraud <= 15 and total >= 3:
            score += 10
            reasons.append("clean fraud history")

        if score > 0:
            scored.append((score, c, reasons))

    scored.sort(key=lambda x: -x[0])
    return [
        {"creator": c, "match": min(100, s), "reasons": r}
        for s, c, r in scored[:limit]
    ]
