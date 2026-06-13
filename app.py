"""Clipscash — content rewards marketplace (Flask MVP).

Run:
    python app.py
    # then open http://127.0.0.1:5000
"""
from __future__ import annotations
import json
import os
import re
import secrets
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import ai_insights
import fraud as fraud_mod
import mailer
from i18n import t, cat_label, CATEGORIES, PLATFORMS, PAYOUT_TYPES

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Public base URL for building absolute links in emails (no trailing slash)
BASE_URL = os.environ.get("CLIPSCASH_BASE_URL", "https://clipscash.app").rstrip("/")

# Cache-busting version for static assets (mtime of styles.css). Changes on every
# deploy that touches CSS, forcing browsers to fetch the fresh file.
try:
    ASSET_VERSION = str(int((ROOT / "static" / "css" / "styles.css").stat().st_mtime))
except Exception:
    ASSET_VERSION = "1"

app = Flask(__name__)

# === Production-aware config ===
IS_PROD = os.environ.get("CLIPSCASH_ENV", "dev").lower() in ("prod", "production")
BEHIND_PROXY = os.environ.get("CLIPSCASH_BEHIND_PROXY", "0").lower() in ("1", "true", "yes") or IS_PROD

# Persistent SECRET_KEY: prefer env var, else use/create .secret_key file
_key_file = ROOT / ".secret_key"
_secret = os.environ.get("CLIPSCASH_SECRET")
if not _secret:
    if _key_file.exists():
        _secret = _key_file.read_text(encoding="utf-8").strip()
    else:
        _secret = secrets.token_hex(32)
        _key_file.write_text(_secret, encoding="utf-8")
        try:
            os.chmod(_key_file, 0o600)
        except Exception:
            pass
app.config["SECRET_KEY"] = _secret
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Auto-secure cookies in production; can be overridden via env
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("CLIPSCASH_HTTPS", "1" if IS_PROD else "0").lower()
    in ("1", "true", "yes")
)
app.config["PREFERRED_URL_SCHEME"] = "https" if IS_PROD else "http"

# When deployed behind nginx / Cloudflare / Hostinger Passenger, trust the
# X-Forwarded-* headers so request.remote_addr returns the real client IP
# (needed for accurate rate limiting) and url_for(_external=True) uses https.
if BEHIND_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.teardown_appcontext(db.close_db)


# ============================================================
# Helpers
# ============================================================

def current_user():
    if "user_id" not in session:
        return None
    if "_user_cache" in g and g._user_cache and g._user_cache["id"] == session["user_id"]:
        return g._user_cache
    row = db.query_one("SELECT * FROM users WHERE id = ?", (session["user_id"],))
    # Invalidate session if user was banned or deleted while logged in
    if row is None or row["banned"]:
        session.clear()
        g._user_cache = None
        return None
    g._user_cache = row
    return row


def safe_next(url: str | None) -> str:
    """Allow only same-origin relative paths to prevent open-redirect via ?next=."""
    if not url:
        return url_for("dashboard")
    # Reject absolute URLs and protocol-relative (//evil.com)
    if url.startswith("//") or "://" in url or url.startswith("\\"):
        return url_for("dashboard")
    if not url.startswith("/"):
        return url_for("dashboard")
    return url


# --- In-memory rate limiter (sliding window) ---
_RATE_BUCKETS: dict[str, list[float]] = {}


def rate_limited(key: str, max_calls: int, window_seconds: int) -> bool:
    """Return True if the key has exceeded max_calls within window_seconds."""
    import time
    now = time.time()
    bucket = _RATE_BUCKETS.setdefault(key, [])
    cutoff = now - window_seconds
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= max_calls:
        return True
    bucket.append(now)
    return False


def lang() -> str:
    if request.cookies.get("lang") in ("ar", "en"):
        return request.cookies.get("lang")
    u = current_user()
    if u and u["lang"] in ("ar", "en"):
        return u["lang"]
    return "ar"


def login_required(role: str | None = None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                if request.path.startswith("/api/"):
                    return jsonify(error="auth_required"), 401
                flash(t("auth_required", lang()), "error")
                return redirect(url_for("login", next=request.path))
            if role and u["role"] != role and u["role"] != "admin":
                if request.path.startswith("/api/"):
                    return jsonify(error="forbidden"), 403
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def fmt_money(cents: int, lang_: str = None) -> str:
    """Format integer halalas as SAR. Symbol: ر.س (Arabic) / SAR (English)."""
    lang_ = lang_ or lang()
    if cents is None:
        cents = 0
    val = cents / 100
    label = "ر.س" if lang_ == "ar" else "SAR"
    if abs(val) >= 1000:
        amount = f"{val:,.0f}"
    else:
        amount = f"{val:,.2f}"
    return f"{amount} {label}"


def fmt_num(n: int) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def parse_money_input(raw: str) -> int:
    """Accept '2.50', '2,50', '2' → cents."""
    if not raw:
        return 0
    raw = raw.replace(",", ".").strip()
    try:
        return int(round(float(raw) * 100))
    except (ValueError, TypeError):
        return 0


def detect_platform(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if "tiktok" in host:
        return "tiktok"
    if "instagram" in host:
        return "reels"
    if "youtube" in host or "youtu.be" in host:
        return "shorts"
    if "twitter" in host or "x.com" in host:
        return "x"
    return ""


URL_RE = re.compile(r"^https?://[^\s]+$", re.I)


def campaign_is_open(c) -> bool:
    """A campaign accepts submissions only when active AND not past its end date
    AND has budget remaining."""
    if not c or c["status"] != "active":
        return False
    if c["ends_at"]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # ends_at stored as YYYY-MM-DD (date input); compare lexically (safe for ISO)
        if str(c["ends_at"])[:10] < today:
            return False
    if c["budget_cents"] - c["spent_cents"] <= 0:
        return False
    return True


def notify(user_id: int, title: str, body: str = "", link: str = "", icon: str = "bell",
           email: bool = False):
    """Create an in-app notification. If email=True and the user has a verified
    email, also send it via SMTP (best-effort; never blocks the request)."""
    db.execute(
        "INSERT INTO notifications (user_id, title, body, link, icon) VALUES (?,?,?,?,?)",
        (user_id, title, body, link, icon),
    )
    if email:
        try:
            row = db.query_one(
                "SELECT email, name, lang, email_verified FROM users WHERE id=?", (user_id,)
            )
            if row and row["email"] and row["email_verified"]:
                full_link = link if str(link).startswith("http") else (BASE_URL + link if link else BASE_URL)
                subject, html = mailer.render_notification_email(
                    row["name"], title, body, full_link, row["lang"] or "ar"
                )
                mailer.send_email(row["email"], subject, html)
        except Exception as _e:
            print(f"[notify:email] failed for user {user_id}: {_e}")


# ============================================================
# Template globals
# ============================================================

@app.template_filter("from_json")
def _from_json_filter(s):
    try:
        return json.loads(s) if s else {}
    except (TypeError, ValueError):
        return {}


# ===== CSRF protection =====
CSRF_EXEMPT_PREFIXES = ("/api/",)


def _csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def _csrf_check():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if any(request.path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
        return
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf", "")
    if not expected or not sent or not secrets.compare_digest(sent, expected):
        abort(403, description="CSRF token missing or invalid")


@app.context_processor
def inject_globals():
    u = current_user()
    unread = 0
    if u:
        row = db.query_one(
            "SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND is_read=0",
            (u["id"],),
        )
        unread = row["c"] if row else 0
    return {
        "t": lambda k: t(k, lang()),
        "lang": lang(),
        "rtl": lang() == "ar",
        "current_user": u,
        "fmt_money": fmt_money,
        "fmt_num": fmt_num,
        "cat_label": lambda c: cat_label(c, lang()),
        "unread_count": unread,
        "now": datetime.now(timezone.utc).replace(tzinfo=None),
        "categories": CATEGORIES,
        "platforms": PLATFORMS,
        "payout_types": PAYOUT_TYPES,
        "csrf_token": _csrf_token(),
        "asset_v": ASSET_VERSION,
    }


# ============================================================
# Public routes
# ============================================================

@app.route("/")
def index():
    """Marketing landing page (neon design). Logged-in users go to dashboard."""
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/campaigns/<int:cid>")
@login_required()
def campaign_detail(cid):
    """Campaign details — login-required. Creators can view ANY active campaign
    (marketplace model). Brands only see their own; admin sees all."""
    u = current_user()
    c = db.query_one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not c:
        abort(404)
    if u["role"] == "brand" and c["brand_id"] != u["id"]:
        abort(403)
    if u["role"] == "creator" and c["status"] != "active":
        # Creators can only see active campaigns (no drafts/ended from other brands)
        abort(404)
    brand = db.query_one("SELECT id,name,avatar_url FROM users WHERE id=?", (c["brand_id"],))
    stats = db.query_one(
        "SELECT COUNT(*) AS subs, COALESCE(SUM(verified_views),0) AS views FROM submissions WHERE campaign_id=? AND status IN ('approved','paid')",
        (cid,),
    )
    return render_template("campaign_detail.html", c=c, brand=brand, stats=stats)


@app.route("/set-lang/<l>")
def set_lang(l):
    if l not in ("ar", "en"):
        l = "ar"
    resp = redirect(request.referrer or url_for("index"))
    resp.set_cookie("lang", l, max_age=60 * 60 * 24 * 365)
    u = current_user()
    if u:
        db.execute("UPDATE users SET lang=? WHERE id=?", (l, u["id"]))
    return resp


# ============================================================
# Auth
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        email = (request.form.get("email") or "").strip().lower()
        if rate_limited(f"login:ip:{ip}", max_calls=10, window_seconds=300) or \
           rate_limited(f"login:email:{email}", max_calls=5, window_seconds=300):
            flash("Too many login attempts. Try again in a few minutes." if lang() == "en"
                  else "محاولات تسجيل دخول كثيرة. حاول بعد دقائق.", "error")
            return render_template("auth/login.html", email=email), 429
        password = request.form.get("password") or ""
        row = db.query_one("SELECT * FROM users WHERE email=?", (email,))
        if not row or not check_password_hash(row["password_hash"], password):
            flash(t("auth_invalid", lang()), "error")
            return render_template("auth/login.html", email=email)
        if row["banned"]:
            msg = "تم تعليق حسابك" if lang() == "ar" else "Your account has been suspended"
            if row["banned_reason"]:
                msg += f": {row['banned_reason']}"
            flash(msg, "error")
            return render_template("auth/login.html", email=email)
        session.clear()
        session["user_id"] = row["id"]
        return redirect(safe_next(request.args.get("next")))
    return render_template("auth/login.html")


# Public registration is disabled — closed B2B platform.
# Brands are created by admin (/admin/brands/new).
# Creators are created by their brand (/brand/team/new).
@app.route("/register")
def register():
    return redirect(url_for("login"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# ============================================================
# Dashboard routing
# ============================================================

@app.route("/dashboard")
@login_required()
def dashboard():
    u = current_user()
    if u["role"] == "creator":
        return redirect(url_for("creator_dashboard"))
    if u["role"] == "brand":
        return redirect(url_for("brand_dashboard"))
    if u["role"] == "admin":
        return redirect(url_for("admin_index"))
    if u["role"] == "fan":
        return redirect(url_for("fan_dashboard"))
    return redirect(url_for("index"))


# ============================================================
# Creator dashboard
# ============================================================

@app.route("/creator")
@login_required("creator")
def creator_dashboard():
    u = current_user()
    subs = db.query(
        """SELECT s.*, c.title AS campaign_title, c.brand_name
           FROM submissions s JOIN campaigns c ON c.id=s.campaign_id
           WHERE s.creator_id=? ORDER BY s.created_at DESC LIMIT 10""",
        (u["id"],),
    )
    counts = {
        "pending": db.query_one("SELECT COUNT(*) c FROM submissions WHERE creator_id=? AND status='pending'", (u["id"],))["c"],
        "approved": db.query_one("SELECT COUNT(*) c FROM submissions WHERE creator_id=? AND status IN ('approved','paid')", (u["id"],))["c"],
        "rejected": db.query_one("SELECT COUNT(*) c FROM submissions WHERE creator_id=? AND status='rejected'", (u["id"],))["c"],
    }
    # Marketplace: show all active campaigns from any brand
    open_campaigns = db.query(
        "SELECT * FROM campaigns WHERE status='active' ORDER BY featured DESC, created_at DESC LIMIT 6"
    )
    return render_template("creator/dashboard.html", subs=subs, counts=counts,
                           brand=None, open_campaigns=open_campaigns)


@app.route("/creator/campaigns")
@login_required("creator")
def creator_campaigns():
    """Marketplace view: all active campaigns from all brands."""
    cat = request.args.get("category", "")
    plat = request.args.get("platform", "")
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "newest")

    sql = "SELECT * FROM campaigns WHERE status='active'"
    params: list = []
    if cat and cat in CATEGORIES:
        sql += " AND category = ?"
        params.append(cat)
    if plat and plat in PLATFORMS:
        sql += " AND platforms LIKE ?"
        params.append(f"%{plat}%")
    if q:
        sql += " AND (title LIKE ? OR brand_name LIKE ? OR description LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    if sort == "highest":
        sql += " ORDER BY payout_rate_cents DESC"
    elif sort == "budget":
        sql += " ORDER BY budget_cents DESC"
    else:
        sql += " ORDER BY featured DESC, created_at DESC"
    sql += " LIMIT 60"
    campaigns = db.query(sql, tuple(params))
    return render_template("creator/campaigns.html", campaigns=campaigns,
                           selected_cat=cat, selected_plat=plat, q=q, sort=sort)


@app.route("/creator/submissions")
@login_required("creator")
def creator_submissions():
    u = current_user()
    status = request.args.get("status", "")
    sql = """SELECT s.*, c.title AS campaign_title, c.brand_name, c.payout_type
             FROM submissions s JOIN campaigns c ON c.id=s.campaign_id
             WHERE s.creator_id=?"""
    params: list = [u["id"]]
    if status in ("pending", "approved", "rejected", "paid"):
        sql += " AND s.status=?"
        params.append(status)
    sql += " ORDER BY s.created_at DESC"
    subs = db.query(sql, tuple(params))
    return render_template("creator/submissions.html", subs=subs, status_f=status)


@app.route("/creator/submissions/<int:sid>/withdraw", methods=["POST"])
@login_required("creator")
def creator_withdraw_submission(sid):
    """Creator cancels their own still-pending submission."""
    u = current_user()
    s = db.query_one(
        "SELECT id, status FROM submissions WHERE id=? AND creator_id=?", (sid, u["id"])
    )
    if not s:
        abort(404)
    if s["status"] != "pending":
        flash("لا يمكن سحب تقديم تمت مراجعته." if lang() == "ar"
              else "Cannot withdraw a reviewed submission.", "error")
        return redirect(url_for("creator_submissions"))
    db.execute("DELETE FROM submissions WHERE id=?", (sid,))
    flash("تم سحب التقديم." if lang() == "ar" else "Submission withdrawn.", "success")
    return redirect(url_for("creator_submissions"))


@app.route("/creator/wallet")
@login_required("creator")
def creator_wallet():
    u = current_user()
    tx = db.query("SELECT * FROM wallet_tx WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (u["id"],))
    payouts = db.query("SELECT * FROM payouts WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (u["id"],))
    return render_template("creator/wallet.html", tx=tx, payouts=payouts)


_PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,18}$")


def _normalize_ksa_phone(raw: str) -> str | None:
    """Accept 0512345678, 512345678, +966512345678, 966512345678 →
    return canonical +9665XXXXXXXX, or None if invalid."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if not s:
        return None
    if s.startswith("+966"):
        digits = s[4:]
    elif s.startswith("966"):
        digits = s[3:]
    elif s.startswith("0"):
        digits = s[1:]
    else:
        digits = s
    if len(digits) == 9 and digits.startswith("5"):
        return "+966" + digits
    # Non-KSA: accept as-is if it looks like an international number
    if _PHONE_RE.match(raw or ""):
        return raw.strip()
    return None


@app.route("/creator/wallet/withdraw", methods=["POST"])
@login_required("creator")
def creator_withdraw():
    u = current_user()
    amount = parse_money_input(request.form.get("amount", ""))
    if amount < 1000:
        flash("Minimum withdrawal is $10.", "error")
        return redirect(url_for("creator_wallet"))
    phone_raw = (request.form.get("phone") or "").strip()
    phone = _normalize_ksa_phone(phone_raw)
    if not phone:
        flash("أدخل رقم جوال صحيح (٠٥X XXX XXXX)." if lang() == "ar"
              else "Enter a valid phone number.", "error")
        return redirect(url_for("creator_wallet"))
    preferred = (request.form.get("preferred") or "").strip()[:60]
    note = (request.form.get("note") or "").strip()[:500]
    method = "contact"
    details = json.dumps({
        "type": "contact",
        "phone": phone,
        "preferred": preferred,
        "note": note,
    }, ensure_ascii=False)
    human_summary = f"{phone}" + (f" · {preferred}" if preferred else "")

    # Atomic conditional decrement — race-safe. Multiple concurrent withdrawals
    # can no longer overdraw because the WHERE clause is checked at UPDATE time.
    changed = db.execute_returning_rowcount(
        "UPDATE users SET balance_cents = balance_cents - ? WHERE id=? AND balance_cents >= ?",
        (amount, u["id"], amount),
    )
    if changed == 0:
        flash("Amount exceeds your balance." if lang() == "en"
              else "المبلغ يتجاوز رصيدك.", "error")
        return redirect(url_for("creator_wallet"))
    pid = db.execute(
        "INSERT INTO payouts (user_id, amount_cents, method, details) VALUES (?,?,?,?)",
        (u["id"], amount, method, details),
    )
    db.execute(
        "INSERT INTO wallet_tx (user_id, kind, amount_cents, note, ref_id) VALUES (?,?,?,?,?)",
        (u["id"], "withdrawal", -amount, f"Withdrawal via {human_summary}", pid),
    )
    notify(u["id"], "Withdrawal requested" if lang() == "en" else "تم طلب السحب",
           f"{fmt_money(amount)} via {method}", url_for("creator_wallet"), "wallet")
    flash("Withdrawal requested.", "success")
    return redirect(url_for("creator_wallet"))


# ----- Submission wizard -----

WIZ_SESS_KEY = "sub_wiz"


@app.route("/campaigns/<int:cid>/submit")
@login_required("creator")
def submit_step1(cid):
    u = current_user()
    c = db.query_one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not campaign_is_open(c):
        flash("هذه الحملة لم تعد تستقبل تقديمات." if lang() == "ar"
              else "This campaign is no longer accepting submissions.", "error")
        return redirect(url_for("creator_campaigns"))
    # Prevent duplicate submission to the same campaign
    dup = db.query_one(
        "SELECT id FROM submissions WHERE campaign_id=? AND creator_id=? AND status IN ('pending','approved','paid')",
        (cid, u["id"]),
    )
    if dup:
        flash("لقد قدّمت على هذه الحملة بالفعل." if lang() == "ar"
              else "You have already submitted to this campaign.", "info")
        return redirect(url_for("creator_submissions"))
    session.pop(WIZ_SESS_KEY, None)
    return render_template("creator/submit_step1.html", c=c, step=1)


@app.route("/campaigns/<int:cid>/submit/back/<int:step>")
@login_required("creator")
def submit_back(cid, step):
    """Render an earlier wizard step using preserved session state."""
    c = db.query_one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not campaign_is_open(c):
        abort(404)
    state = session.get(WIZ_SESS_KEY, {})
    tpl = {1: "creator/submit_step1.html",
           2: "creator/submit_step2.html",
           3: "creator/submit_step3.html"}.get(step)
    if not tpl:
        return redirect(url_for("submit_step1", cid=cid))
    return render_template(tpl, c=c, step=step, state=state)


@app.route("/campaigns/<int:cid>/submit/step/<int:step>", methods=["POST"])
@login_required("creator")
def submit_step(cid, step):
    c = db.query_one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not campaign_is_open(c):
        flash("هذه الحملة لم تعد تستقبل تقديمات." if lang() == "ar"
              else "This campaign is no longer accepting submissions.", "error")
        return redirect(url_for("creator_campaigns"))
    state = session.get(WIZ_SESS_KEY, {})

    if step == 1:
        if not request.form.get("agree"):
            flash("Please confirm eligibility.", "error")
            return render_template("creator/submit_step1.html", c=c, step=1)
        return render_template("creator/submit_step2.html", c=c, step=2, state=state)

    if step == 2:
        url = (request.form.get("video_url") or "").strip()
        if not URL_RE.match(url):
            flash("Enter a valid video URL.", "error")
            return render_template("creator/submit_step2.html", c=c, step=2, state=state)
        plat = detect_platform(url) or "tiktok"
        state.update({"video_url": url, "platform": plat,
                      "caption": (request.form.get("caption") or "").strip()})
        session[WIZ_SESS_KEY] = state
        return render_template("creator/submit_step3.html", c=c, step=3, state=state)

    if step == 3:
        proofs = []
        for i in (1, 2, 3):
            f = request.files.get(f"proof_{i}")
            if f and f.filename:
                ext = secure_filename(f.filename).rsplit(".", 1)[-1].lower()
                if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
                    flash("Only image files allowed.", "error")
                    return render_template("creator/submit_step3.html", c=c, step=3, state=state)
                fname = f"sub_{secrets.token_hex(8)}.{ext}"
                f.save(UPLOAD_DIR / fname)
                proofs.append(f"/static/uploads/{fname}")
            else:
                proofs.append(None)
        state["proofs"] = proofs
        session[WIZ_SESS_KEY] = state
        return render_template("creator/submit_step4.html", c=c, step=4, state=state)

    if step == 4:
        try:
            views = int(request.form.get("self_views") or 0)
            likes = int(request.form.get("self_likes") or 0)
            comments = int(request.form.get("self_comments") or 0)
        except ValueError:
            flash("Stats must be numbers.", "error")
            return render_template("creator/submit_step4.html", c=c, step=4, state=state)
        u = current_user()
        # Server-side duplicate guard (defends against double-submit / direct POST)
        dup = db.query_one(
            "SELECT id FROM submissions WHERE campaign_id=? AND creator_id=? AND status IN ('pending','approved','paid')",
            (cid, u["id"]),
        )
        if dup:
            session.pop(WIZ_SESS_KEY, None)
            flash("لقد قدّمت على هذه الحملة بالفعل." if lang() == "ar"
                  else "You have already submitted to this campaign.", "info")
            return redirect(url_for("creator_submissions"))
        priors = db.query_one(
            """SELECT
                COALESCE(SUM(CASE WHEN status IN ('approved','paid') THEN 1 ELSE 0 END),0) AS app,
                COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END),0) AS rej
               FROM submissions WHERE creator_id=?""",
            (u["id"],),
        )
        proofs = state.get("proofs") or [None, None, None]
        proof_count = sum(1 for p in proofs if p)
        created = datetime.strptime(u["created_at"][:19], "%Y-%m-%d %H:%M:%S")
        age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - created).days
        fscore, _ = fraud_mod.compute_fraud_score(
            views, likes, comments,
            proof_count=proof_count, creator_age_days=age_days,
            prior_approved=priors["app"], prior_rejected=priors["rej"],
        )
        share_token = secrets.token_urlsafe(8)
        sid = db.execute(
            """INSERT INTO submissions
               (campaign_id, creator_id, video_url, platform, caption,
                proof_url_1, proof_url_2, proof_url_3,
                self_views, self_likes, self_comments, fraud_score, status, share_token)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
            (cid, u["id"], state.get("video_url"), state.get("platform"),
             state.get("caption"), proofs[0], proofs[1], proofs[2],
             views, likes, comments, fscore, share_token),
        )
        notify(c["brand_id"],
               "New submission" if lang() == "en" else "تقديم جديد",
               f"{u['name']} submitted to '{c['title']}'",
               url_for("brand_review", sid=sid), "inbox")
        session.pop(WIZ_SESS_KEY, None)
        flash("Submission received! The brand will review shortly.", "success")
        return redirect(url_for("creator_submissions"))

    abort(400)


# ============================================================
# Brand dashboard
# ============================================================

@app.route("/brand")
@login_required("brand")
def brand_dashboard():
    u = current_user()
    campaigns = db.query(
        "SELECT * FROM campaigns WHERE brand_id=? ORDER BY created_at DESC", (u["id"],)
    )
    counts = {
        "active": sum(1 for c in campaigns if c["status"] == "active"),
        "draft": sum(1 for c in campaigns if c["status"] == "draft"),
        "ended": sum(1 for c in campaigns if c["status"] == "ended"),
    }
    pending_subs = db.query_one(
        """SELECT COUNT(*) c FROM submissions s
           JOIN campaigns c ON c.id=s.campaign_id
           WHERE c.brand_id=? AND s.status='pending'""",
        (u["id"],),
    )["c"]
    return render_template("brand/dashboard.html", campaigns=campaigns,
                           counts=counts, pending_subs=pending_subs)


@app.route("/brand/campaigns")
@login_required("brand")
def brand_campaigns():
    u = current_user()
    campaigns = db.query(
        "SELECT * FROM campaigns WHERE brand_id=? ORDER BY created_at DESC", (u["id"],)
    )
    return render_template("brand/campaigns.html", campaigns=campaigns)


@app.route("/brand/campaigns/<int:cid>")
@login_required("brand")
def brand_campaign_detail(cid):
    u = current_user()
    c = db.query_one("SELECT * FROM campaigns WHERE id=? AND brand_id=?", (cid, u["id"]))
    if not c:
        abort(404)
    tab = request.args.get("tab", "overview")
    subs = db.query(
        """SELECT s.*, u.name AS creator_name, u.avatar_url
           FROM submissions s JOIN users u ON u.id=s.creator_id
           WHERE s.campaign_id=? ORDER BY s.created_at DESC""",
        (cid,),
    )
    return render_template("brand/campaign_detail.html", c=c, subs=subs, tab=tab)


# ----- Campaign creation wizard (5 steps) -----

CAMP_WIZ_KEY = "camp_wiz"


@app.route("/brand/campaigns/new")
@login_required("brand")
def camp_wiz_start():
    session.pop(CAMP_WIZ_KEY, None)
    session[CAMP_WIZ_KEY] = {}
    return render_template("brand/wizard_step1.html", step=1, state={})


@app.route("/brand/campaigns/new/back/<int:step>")
@login_required("brand")
def camp_wiz_back(step):
    """Render an earlier wizard step using preserved session state."""
    state = session.get(CAMP_WIZ_KEY, {})
    tpl = {1: "brand/wizard_step1.html",
           2: "brand/wizard_step2.html",
           3: "brand/wizard_step3.html",
           4: "brand/wizard_step4.html"}.get(step)
    if not tpl:
        return redirect(url_for("camp_wiz_start"))
    return render_template(tpl, step=step, state=state)


@app.route("/brand/campaigns/new/step/<int:step>", methods=["POST"])
@login_required("brand")
def camp_wiz_step(step):
    state = session.get(CAMP_WIZ_KEY, {})
    action = request.form.get("action", "next")

    if step == 1:
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        category = request.form.get("category", "other")
        image_url = (request.form.get("image_url") or "").strip()
        f = request.files.get("image_file")
        if f and f.filename:
            ext = secure_filename(f.filename).rsplit(".", 1)[-1].lower()
            if ext in ("png", "jpg", "jpeg", "webp", "gif"):
                fname = f"camp_{secrets.token_hex(8)}.{ext}"
                f.save(UPLOAD_DIR / fname)
                image_url = f"/static/uploads/{fname}"
        if not title or not description or category not in CATEGORIES:
            flash("Title, description, and category are required.", "error")
            return render_template("brand/wizard_step1.html", step=1, state=state)
        state.update({"title": title, "description": description,
                      "category": category, "image_url": image_url})
        session[CAMP_WIZ_KEY] = state
        return render_template("brand/wizard_step2.html", step=2, state=state)

    if step == 2:
        ptype = request.form.get("payout_type", "per_view")
        rate = parse_money_input(request.form.get("payout_rate", "0"))
        if ptype not in PAYOUT_TYPES or rate <= 0:
            flash("Pick a payout type and enter a positive rate.", "error")
            return render_template("brand/wizard_step2.html", step=2, state=state)
        state.update({"payout_type": ptype, "payout_rate_cents": rate})
        session[CAMP_WIZ_KEY] = state
        return render_template("brand/wizard_step3.html", step=3, state=state)

    if step == 3:
        plats = request.form.getlist("platforms")
        plats = [p for p in plats if p in PLATFORMS]
        if not plats:
            flash("Choose at least one platform.", "error")
            return render_template("brand/wizard_step3.html", step=3, state=state)
        state.update({
            "platforms": ",".join(plats),
            "hashtags": (request.form.get("hashtags") or "").strip(),
            "mentions": (request.form.get("mentions") or "").strip(),
            "min_duration": int(request.form.get("min_duration") or 15),
            "max_duration": int(request.form.get("max_duration") or 90),
            "example_links": (request.form.get("example_links") or "").strip(),
            "brief": (request.form.get("brief") or state.get("description", "")).strip(),
        })
        session[CAMP_WIZ_KEY] = state
        return render_template("brand/wizard_step4.html", step=4, state=state)

    if step == 4:
        budget = parse_money_input(request.form.get("budget", "0"))
        min_payout = parse_money_input(request.form.get("min_payout", "1"))
        starts_at = (request.form.get("starts_at") or "").strip() or None
        ends_at = (request.form.get("ends_at") or "").strip() or None
        if budget <= 0:
            flash("Budget must be positive.", "error")
            return render_template("brand/wizard_step4.html", step=4, state=state)
        state.update({
            "budget_cents": budget, "min_payout_cents": min_payout,
            "starts_at": starts_at, "ends_at": ends_at,
        })
        session[CAMP_WIZ_KEY] = state
        return render_template("brand/wizard_step5.html", step=5, state=state)

    if step == 5:
        u = current_user()
        is_draft = action == "draft"
        status = "draft" if is_draft else "active"
        if not is_draft:
            # Atomic balance check + deduct, race-safe.
            need = state.get("budget_cents", 0)
            changed = db.execute_returning_rowcount(
                "UPDATE users SET balance_cents = balance_cents - ? WHERE id=? AND balance_cents >= ?",
                (need, u["id"], need),
            )
            if changed == 0:
                flash("Insufficient wallet balance — top up first or save as draft.", "error")
                return render_template("brand/wizard_step5.html", step=5, state=state)
            db.execute(
                "INSERT INTO wallet_tx (user_id, kind, amount_cents, note) VALUES (?,?,?,?)",
                (u["id"], "charge", -need, f"Funded campaign: {state['title']}"),
            )
        cid = db.execute(
            """INSERT INTO campaigns
               (brand_id, title, brand_name, description, brief, category,
                platforms, hashtags, mentions, min_duration, max_duration,
                example_links, payout_type, payout_rate_cents, budget_cents,
                min_payout_cents, image_url, status, starts_at, ends_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (u["id"], state["title"], u["name"], state["description"],
             state.get("brief", state["description"]), state["category"],
             state["platforms"], state.get("hashtags"), state.get("mentions"),
             state.get("min_duration", 15), state.get("max_duration", 90),
             state.get("example_links"), state["payout_type"], state["payout_rate_cents"],
             state["budget_cents"], state.get("min_payout_cents", 100),
             state.get("image_url"), status, state.get("starts_at"), state.get("ends_at")),
        )
        session.pop(CAMP_WIZ_KEY, None)
        flash("Campaign saved as draft." if is_draft else "Campaign launched!", "success")
        return redirect(url_for("brand_campaign_detail", cid=cid))

    abort(400)


# ----- Submission review (3-column screen) -----

@app.route("/brand/submissions/<int:sid>")
@login_required("brand")
def brand_review(sid):
    u = current_user()
    s = db.query_one(
        """SELECT s.*, c.brand_id, c.title AS campaign_title, c.payout_type,
                  c.payout_rate_cents, c.min_payout_cents,
                  cr.name AS creator_name, cr.avatar_url, cr.created_at AS creator_since,
                  cr.id AS creator_uid
           FROM submissions s
           JOIN campaigns c ON c.id=s.campaign_id
           JOIN users cr ON cr.id=s.creator_id
           WHERE s.id=?""",
        (sid,),
    )
    if not s or s["brand_id"] != u["id"]:
        abort(404)
    creator_stats = db.query_one(
        """SELECT
            SUM(CASE WHEN status IN ('approved','paid') THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
            COUNT(*) AS total
           FROM submissions WHERE creator_id=?""",
        (s["creator_uid"],),
    )
    trust = db.query_one(
        "SELECT mark FROM trust_marks WHERE brand_id=? AND creator_id=?",
        (u["id"], s["creator_uid"]),
    )
    return render_template("brand/review.html", s=s, creator_stats=creator_stats,
                           trust=trust["mark"] if trust else None)


def _payout_for(verified_views: int, verified_likes: int, verified_comments: int,
                payout_type: str, rate_cents: int) -> int:
    if payout_type == "per_view":
        return int(verified_views / 1000.0 * rate_cents)
    if payout_type == "per_post":
        return rate_cents
    if payout_type == "per_engagement":
        return (verified_likes + verified_comments) * rate_cents
    if payout_type == "hybrid":
        return int(verified_views / 1000.0 * rate_cents) + (verified_likes + verified_comments) * (rate_cents // 10 or 1)
    return 0


@app.route("/api/payout-preview", methods=["POST"])
@login_required("brand")
def api_payout_preview():
    data = request.get_json(silent=True) or {}
    payout = _payout_for(
        int(data.get("views") or 0),
        int(data.get("likes") or 0),
        int(data.get("comments") or 0),
        data.get("payout_type") or "per_view",
        int(data.get("rate_cents") or 0),
    )
    return jsonify(payout_cents=payout, payout_display=fmt_money(payout))


@app.route("/brand/submissions/<int:sid>/approve", methods=["POST"])
@login_required("brand")
def brand_approve(sid):
    u = current_user()
    s = db.query_one(
        """SELECT s.*, c.brand_id, c.status AS campaign_status, c.payout_type,
                  c.payout_rate_cents, c.budget_cents, c.spent_cents,
                  c.min_payout_cents, c.id AS cid
           FROM submissions s JOIN campaigns c ON c.id=s.campaign_id
           WHERE s.id=?""",
        (sid,),
    )
    if not s or s["brand_id"] != u["id"] or s["status"] != "pending":
        abort(404)
    if s["campaign_status"] not in ("active", "paused"):
        flash("لا يمكن الموافقة — الحملة منتهية." if lang() == "ar"
              else "Cannot approve — campaign has ended.", "error")
        return redirect(url_for("brand_review", sid=sid))
    try:
        vv = int(request.form.get("verified_views") or 0)
        vl = int(request.form.get("verified_likes") or 0)
        vc = int(request.form.get("verified_comments") or 0)
    except ValueError:
        flash("Verified stats must be numeric.", "error")
        return redirect(url_for("brand_review", sid=sid))
    payout = _payout_for(vv, vl, vc, s["payout_type"], s["payout_rate_cents"])
    # Enforce per-submission minimum payout floor (if computed > 0)
    min_payout = s["min_payout_cents"] or 0
    if 0 < payout < min_payout:
        payout = min_payout
    remaining = s["budget_cents"] - s["spent_cents"]
    if payout > remaining:
        payout = remaining
    if payout < 0:
        payout = 0
    # Atomic, race-safe budget charge: only succeeds if budget still covers it.
    charged = db.execute_returning_rowcount(
        "UPDATE campaigns SET spent_cents = spent_cents + ? WHERE id=? AND spent_cents + ? <= budget_cents",
        (payout, s["cid"], payout),
    )
    if charged == 0:
        flash("تجاوزت ميزانية الحملة بسبب موافقة متزامنة — حدّث الصفحة." if lang() == "ar"
              else "Campaign budget changed (concurrent approval). Refresh and retry.", "error")
        return redirect(url_for("brand_review", sid=sid))
    db.execute(
        """UPDATE submissions SET status='approved', verified_views=?, verified_likes=?,
           verified_comments=?, earnings_cents=?, reviewed_at=datetime('now'),
           review_note=? WHERE id=?""",
        (vv, vl, vc, payout, request.form.get("note", ""), sid),
    )
    db.execute("UPDATE users SET balance_cents = balance_cents + ?, total_paid_cents = total_paid_cents + ? WHERE id=?",
               (payout, payout, s["creator_id"]))
    db.execute(
        "INSERT INTO wallet_tx (user_id, kind, amount_cents, note, ref_id) VALUES (?,?,?,?,?)",
        (s["creator_id"], "earning", payout, "Approved submission earning", sid),
    )
    notify(s["creator_id"],
           "Submission approved" if lang() == "en" else "تمت الموافقة على تقديمك",
           (f"You earned {fmt_money(payout)}" if lang() == "en"
            else f"ربحت {fmt_money(payout)}"),
           url_for("creator_submissions"), "check", email=True)
    flash(f"Approved. {fmt_money(payout)} credited.", "success")
    return redirect(url_for("brand_campaign_detail", cid=s["cid"], tab="submissions"))


@app.route("/brand/submissions/<int:sid>/reject", methods=["POST"])
@login_required("brand")
def brand_reject(sid):
    u = current_user()
    s = db.query_one(
        """SELECT s.*, c.brand_id, c.id AS cid FROM submissions s
           JOIN campaigns c ON c.id=s.campaign_id WHERE s.id=?""",
        (sid,),
    )
    if not s or s["brand_id"] != u["id"] or s["status"] != "pending":
        abort(404)
    note = (request.form.get("note") or "").strip()
    db.execute(
        "UPDATE submissions SET status='rejected', review_note=?, reviewed_at=datetime('now') WHERE id=?",
        (note, sid),
    )
    notify(s["creator_id"],
           "Submission rejected" if lang() == "en" else "تم رفض تقديمك",
           note or ("No reason given" if lang() == "en" else "بدون سبب محدّد"),
           url_for("creator_submissions"), "x", email=True)
    flash("Submission rejected.", "info")
    return redirect(url_for("brand_campaign_detail", cid=s["cid"], tab="submissions"))


@app.route("/brand/creators/<int:creator_id>/mark", methods=["POST"])
@login_required("brand")
def brand_mark_creator(creator_id):
    u = current_user()
    mark = request.form.get("mark", "")
    next_url = request.form.get("next") or url_for("brand_dashboard")
    if mark not in ("trusted", "blocked", "clear"):
        abort(400)
    if mark == "clear":
        db.execute("DELETE FROM trust_marks WHERE brand_id=? AND creator_id=?", (u["id"], creator_id))
    else:
        db.execute(
            """INSERT INTO trust_marks (brand_id, creator_id, mark)
               VALUES (?,?,?)
               ON CONFLICT(brand_id, creator_id) DO UPDATE SET mark=excluded.mark""",
            (u["id"], creator_id, mark),
        )
    flash("Updated.", "success")
    return redirect(next_url)


# ----- Wallet -----

@app.route("/brand/wallet")
@login_required("brand")
def brand_wallet():
    u = current_user()
    tx = db.query("SELECT * FROM wallet_tx WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (u["id"],))
    return render_template("brand/wallet.html", tx=tx)


@app.route("/brand/wallet/topup", methods=["POST"])
@login_required("brand")
def brand_topup():
    u = current_user()
    amount = parse_money_input(request.form.get("amount", ""))
    if amount <= 0:
        flash("Enter a positive amount.", "error")
        return redirect(url_for("brand_wallet"))
    db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE id=?", (amount, u["id"]))
    db.execute(
        "INSERT INTO wallet_tx (user_id, kind, amount_cents, note) VALUES (?,?,?,?)",
        (u["id"], "topup", amount, "Wallet top-up (mock — Stripe coming soon)"),
    )
    flash(f"Added {fmt_money(amount)} to wallet.", "success")
    return redirect(url_for("brand_wallet"))


# ----- AI Insights -----

@app.route("/brand/campaigns/<int:cid>/insights")
@login_required("brand")
def brand_insights(cid):
    u = current_user()
    c = db.query_one("SELECT * FROM campaigns WHERE id=? AND brand_id=?", (cid, u["id"]))
    if not c:
        abort(404)
    subs = db.query("SELECT * FROM submissions WHERE campaign_id=?", (cid,))
    analysis = ai_insights.analyze_campaign(dict(c), [dict(s) for s in subs])

    creators = db.query("""
        SELECT u.id, u.name, u.avatar_url, u.socials, u.country,
               COALESCE(SUM(CASE WHEN s.status IN ('approved','paid') THEN 1 ELSE 0 END),0) AS prior_approved,
               COALESCE(SUM(CASE WHEN s.status='rejected' THEN 1 ELSE 0 END),0) AS prior_rejected,
               COALESCE(AVG(s.verified_views),0) AS avg_views,
               COALESCE(AVG(s.fraud_score),0) AS avg_fraud
        FROM users u LEFT JOIN submissions s ON s.creator_id=u.id
        WHERE u.role='creator'
        GROUP BY u.id
    """)
    matches = ai_insights.suggest_creators(dict(c), [dict(x) for x in creators], limit=10)
    return render_template("brand/insights.html", c=c, analysis=analysis,
                           matches=matches, insight_t=lambda k: ai_insights.insight_text(k, lang()))


# ============================================================
# Profile (shared)
# ============================================================

@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    u = current_user()
    if request.method == "POST":
        name = (request.form.get("name") or u["name"]).strip()
        bio = (request.form.get("bio") or "").strip()
        country = (request.form.get("country") or "").strip()
        socials = {
            "tiktok": (request.form.get("tiktok") or "").strip(),
            "instagram": (request.form.get("instagram") or "").strip(),
            "youtube": (request.form.get("youtube") or "").strip(),
            "x": (request.form.get("x") or "").strip(),
        }
        avatar = u["avatar_url"]
        f = request.files.get("avatar")
        if f and f.filename:
            ext = secure_filename(f.filename).rsplit(".", 1)[-1].lower()
            if ext in ("png", "jpg", "jpeg", "webp"):
                fname = f"avatar_{u['id']}_{secrets.token_hex(4)}.{ext}"
                f.save(UPLOAD_DIR / fname)
                avatar = f"/static/uploads/{fname}"
        db.execute(
            "UPDATE users SET name=?, bio=?, country=?, socials=?, avatar_url=? WHERE id=?",
            (name, bio, country, json.dumps(socials), avatar, u["id"]),
        )
        g.pop("_user_cache", None)
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))
    socials = json.loads(u["socials"]) if u["socials"] else {}
    return render_template("profile.html", u=u, socials=socials)


# ============================================================
# Notifications API
# ============================================================

@app.route("/api/notifications")
@login_required()
def api_notifications():
    u = current_user()
    rows = db.query(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (u["id"],),
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/notifications/unread-count")
@login_required()
def api_unread_count():
    u = current_user()
    row = db.query_one(
        "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
        (u["id"],),
    )
    return jsonify(count=row["c"])


@app.route("/api/notifications/read", methods=["POST"])
@login_required()
def api_notifications_read():
    u = current_user()
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (u["id"],))
    return jsonify(ok=True)


@app.route("/notifications")
@login_required()
def notifications_page():
    u = current_user()
    rows = db.query("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (u["id"],))
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (u["id"],))
    return render_template("notifications.html", notifs=rows)


# ============================================================
# Admin
# ============================================================

@app.route("/admin")
@login_required("admin")
def admin_index():
    stats = {
        "users": db.query_one("SELECT COUNT(*) c FROM users")["c"],
        "creators": db.query_one("SELECT COUNT(*) c FROM users WHERE role='creator'")["c"],
        "brands": db.query_one("SELECT COUNT(*) c FROM users WHERE role='brand'")["c"],
        "campaigns": db.query_one("SELECT COUNT(*) c FROM campaigns")["c"],
        "submissions": db.query_one("SELECT COUNT(*) c FROM submissions")["c"],
        "pending": db.query_one("SELECT COUNT(*) c FROM submissions WHERE status='pending'")["c"],
        "paid": db.query_one("SELECT COALESCE(SUM(earnings_cents),0) s FROM submissions WHERE status IN ('approved','paid')")["s"],
    }
    return render_template("admin/index.html", stats=stats)


@app.route("/admin/users")
@login_required("admin")
def admin_users():
    q = (request.args.get("q") or "").strip()
    role_f = request.args.get("role", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per = 50
    where = "WHERE 1=1"
    params: list = []
    if q:
        where += " AND (name LIKE ? OR email LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if role_f in ("creator", "brand", "admin", "fan"):
        where += " AND role=?"
        params.append(role_f)
    total = db.query_one(f"SELECT COUNT(*) c FROM users {where}", tuple(params))["c"]
    users = db.query(
        f"SELECT * FROM users {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (per, (page - 1) * per),
    )
    pages = max(1, (total + per - 1) // per)
    return render_template("admin/users.html", users=users, q=q, role_f=role_f,
                           page=page, pages=pages, total=total)


@app.route("/admin/campaigns")
@login_required("admin")
def admin_campaigns():
    rows = db.query("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 200")
    return render_template("admin/campaigns.html", rows=rows)


@app.route("/admin/submissions")
@login_required("admin")
def admin_submissions():
    status_f = request.args.get("status", "")
    sql = """SELECT s.*, c.title AS campaign_title, c.brand_name, u.name AS creator_name
             FROM submissions s
             JOIN campaigns c ON c.id=s.campaign_id
             JOIN users u ON u.id=s.creator_id"""
    params: list = []
    if status_f in ("pending", "approved", "rejected", "paid"):
        sql += " WHERE s.status=?"
        params.append(status_f)
    sql += " ORDER BY s.created_at DESC LIMIT 200"
    rows = db.query(sql, tuple(params))
    return render_template("admin/submissions.html", rows=rows, status_f=status_f)


# ===== Brand: TEAM (creators) management =====

@app.route("/brand/team")
@login_required("brand")
def brand_team():
    u = current_user()
    creators = db.query(
        """SELECT u.*,
           (SELECT COUNT(*) FROM submissions WHERE creator_id=u.id) AS total_subs,
           (SELECT COUNT(*) FROM submissions WHERE creator_id=u.id AND status IN ('approved','paid')) AS approved_subs
           FROM users u WHERE u.role='creator' AND u.brand_id=? ORDER BY u.created_at DESC""",
        (u["id"],),
    )
    return render_template("brand/team.html", creators=creators)


@app.route("/brand/team/new")
@login_required("brand")
def brand_team_new():
    """Brands no longer create creators directly — the platform admin does.
    This route now redirects to the team list with an explainer."""
    flash("إضافة المبدعين تتم من قبل إدارة المنصة. اطلب من الأدمن إنشاء حساب المبدع وربطه ببراندك." if lang() == "ar"
          else "Creators are added by the platform admin. Contact the admin to add a creator to your brand.", "info")
    return redirect(url_for("brand_team"))


@app.route("/brand/team/<int:uid>")
@login_required("brand")
def brand_team_detail(uid):
    u = current_user()
    c = db.query_one("SELECT * FROM users WHERE id=? AND brand_id=? AND role='creator'",
                     (uid, u["id"]))
    if not c:
        abort(404)
    subs = db.query(
        """SELECT s.*, cmp.title AS campaign_title FROM submissions s
           JOIN campaigns cmp ON cmp.id=s.campaign_id
           WHERE s.creator_id=? ORDER BY s.created_at DESC LIMIT 30""",
        (uid,),
    )
    socials = json.loads(c["socials"]) if c["socials"] else {}
    return render_template("brand/team_detail.html", c=c, subs=subs, socials=socials)


@app.route("/brand/team/<int:uid>/disable", methods=["POST"])
@login_required("brand")
def brand_team_disable(uid):
    u = current_user()
    c = db.query_one("SELECT id FROM users WHERE id=? AND brand_id=? AND role='creator'",
                     (uid, u["id"]))
    if not c:
        abort(404)
    reason = (request.form.get("reason") or "Disabled by brand").strip()
    db.execute("UPDATE users SET banned=1, banned_reason=?, banned_at=datetime('now') WHERE id=?",
               (reason, uid))
    flash("Creator disabled.", "success")
    return redirect(url_for("brand_team_detail", uid=uid))


@app.route("/brand/team/<int:uid>/enable", methods=["POST"])
@login_required("brand")
def brand_team_enable(uid):
    u = current_user()
    c = db.query_one("SELECT id FROM users WHERE id=? AND brand_id=? AND role='creator'",
                     (uid, u["id"]))
    if not c:
        abort(404)
    db.execute("UPDATE users SET banned=0, banned_reason=NULL, banned_at=NULL WHERE id=?", (uid,))
    flash("Creator enabled.", "success")
    return redirect(url_for("brand_team_detail", uid=uid))


@app.route("/brand/team/<int:uid>/reset-password", methods=["POST"])
@login_required("brand")
def brand_team_reset_password(uid):
    u = current_user()
    c = db.query_one("SELECT id, email FROM users WHERE id=? AND brand_id=? AND role='creator'",
                     (uid, u["id"]))
    if not c:
        abort(404)
    new_pw = (request.form.get("new_password") or "").strip()
    if len(new_pw) < 6:
        flash("Password must be 6+ chars.", "error")
        return redirect(url_for("brand_team_detail", uid=uid))
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (generate_password_hash(new_pw), uid))
    notify(uid, "Password reset" if lang() == "en" else "تم إعادة تعيين كلمة المرور",
           f"By {u['name']}", url_for("login"), "lock")
    flash(f"Password reset for {c['email']}: {new_pw}", "success")
    return redirect(url_for("brand_team_detail", uid=uid))


# ===== Super-admin: USER management =====

@app.route("/admin/creators/new", methods=["GET", "POST"])
@login_required("admin")
def admin_creator_new():
    brands = db.query(
        "SELECT id, name, email FROM users WHERE role='brand' AND banned=0 ORDER BY name"
    )
    if request.method == "POST":
        try:
            brand_id = int(request.form.get("brand_id") or 0) or None
        except ValueError:
            brand_id = None
        if brand_id and not db.query_one("SELECT 1 FROM users WHERE id=? AND role='brand'", (brand_id,)):
            brand_id = None
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "").strip()
        country = (request.form.get("country") or "").strip()
        if not name or not email or len(password) < 6:
            flash("Name, email, and 6+ char password required.", "error")
            return render_template("admin/creator_new.html", brands=brands, name=name, email=email, country=country, brand_id=brand_id)
        if db.query_one("SELECT 1 FROM users WHERE email=?", (email,)):
            flash(t("auth_email_taken", lang()), "error")
            return render_template("admin/creator_new.html", brands=brands, name=name, email=email, country=country, brand_id=brand_id)
        uid = db.execute(
            """INSERT INTO users
               (email,password_hash,name,role,country,lang,brand_id,email_verified)
               VALUES (?,?,?,?,?,?,?,1)""",
            (email, generate_password_hash(password), name, "creator", country, lang(), brand_id),
        )
        if brand_id:
            brand = db.query_one("SELECT name FROM users WHERE id=?", (brand_id,))
            notify(uid, "Welcome" if lang() == "en" else "أهلاً بك",
                   f"Account created by {brand['name']}",
                   url_for("dashboard"), "sparkles")
            flash(f"Creator '{name}' created (sponsor: {brand['name']}). Login: {email} / {password}", "success")
        else:
            notify(uid, "Welcome" if lang() == "en" else "أهلاً بك",
                   "Your creator account is ready.", url_for("dashboard"), "sparkles")
            flash(f"Creator '{name}' created (independent). Login: {email} / {password}", "success")
        return redirect(url_for("admin_user_detail", uid=uid))
    return render_template("admin/creator_new.html", brands=brands)


@app.route("/admin/brands/new", methods=["GET", "POST"])
@login_required("admin")
def admin_brand_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "").strip()
        country = (request.form.get("country") or "").strip()
        initial_balance = parse_money_input(request.form.get("initial_balance", ""))
        if not name or not email or len(password) < 6:
            flash("Name, email, and 6+ char password required.", "error")
            return render_template("admin/brand_new.html", name=name, email=email, country=country)
        if db.query_one("SELECT 1 FROM users WHERE email=?", (email,)):
            flash("Email is already registered.", "error")
            return render_template("admin/brand_new.html", name=name, email=email, country=country)
        uid = db.execute(
            "INSERT INTO users (email,password_hash,name,role,country,balance_cents,lang) VALUES (?,?,?,?,?,?,?)",
            (email, generate_password_hash(password), name, "brand", country, initial_balance, lang()),
        )
        if initial_balance > 0:
            db.execute("INSERT INTO wallet_tx (user_id,kind,amount_cents,note) VALUES (?,?,?,?)",
                       (uid, "topup", initial_balance, "Initial balance by admin"))
        notify(uid, "Welcome" if lang() == "en" else "أهلاً بك",
               f"Brand account created. Login: {email}", url_for("dashboard"), "sparkles")
        flash(f"Brand '{name}' created. Login: {email} / {password}", "success")
        return redirect(url_for("admin_user_detail", uid=uid))
    return render_template("admin/brand_new.html")


@app.route("/admin/users/<int:uid>")
@login_required("admin")
def admin_user_detail(uid):
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        abort(404)
    campaigns = db.query("SELECT * FROM campaigns WHERE brand_id=? ORDER BY created_at DESC LIMIT 20", (uid,))
    submissions = db.query(
        """SELECT s.*, c.title AS campaign_title FROM submissions s
           JOIN campaigns c ON c.id=s.campaign_id
           WHERE s.creator_id=? ORDER BY s.created_at DESC LIMIT 30""",
        (uid,),
    )
    payouts = db.query("SELECT * FROM payouts WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (uid,))
    tx = db.query("SELECT * FROM wallet_tx WHERE user_id=? ORDER BY created_at DESC LIMIT 30", (uid,))
    socials = json.loads(u["socials"]) if u["socials"] else {}
    return render_template("admin/user_detail.html", u=u, campaigns=campaigns,
                           submissions=submissions, payouts=payouts, tx=tx, socials=socials)


@app.route("/admin/users/<int:uid>/ban", methods=["POST"])
@login_required("admin")
def admin_ban_user(uid):
    if uid == current_user()["id"]:
        flash("You cannot ban yourself.", "error")
        return redirect(url_for("admin_user_detail", uid=uid))
    reason = (request.form.get("reason") or "").strip()
    db.execute(
        "UPDATE users SET banned=1, banned_reason=?, banned_at=datetime('now') WHERE id=?",
        (reason, uid),
    )
    notify(uid, "Account suspended" if lang() == "en" else "تم تعليق الحساب",
           reason or "Contact support", "/", "x")
    flash("User banned.", "success")
    return redirect(url_for("admin_user_detail", uid=uid))


@app.route("/admin/users/<int:uid>/unban", methods=["POST"])
@login_required("admin")
def admin_unban_user(uid):
    db.execute("UPDATE users SET banned=0, banned_reason=NULL, banned_at=NULL WHERE id=?", (uid,))
    notify(uid, "Account reinstated" if lang() == "en" else "تم تفعيل الحساب",
           "Welcome back.", "/", "check")
    flash("User unbanned.", "success")
    return redirect(url_for("admin_user_detail", uid=uid))


@app.route("/admin/users/<int:uid>/balance", methods=["POST"])
@login_required("admin")
def admin_adjust_balance(uid):
    sign = 1 if request.form.get("op", "credit") == "credit" else -1
    amount = parse_money_input(request.form.get("amount", "")) * sign
    note = (request.form.get("note") or "Admin adjustment").strip()
    if amount == 0:
        flash("Amount required.", "error")
        return redirect(url_for("admin_user_detail", uid=uid))
    target = db.query_one("SELECT balance_cents FROM users WHERE id=?", (uid,))
    if not target:
        abort(404)
    new_bal = target["balance_cents"] + amount
    if new_bal < 0:
        flash("Adjustment would make balance negative.", "error")
        return redirect(url_for("admin_user_detail", uid=uid))
    db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE id=?", (amount, uid))
    db.execute(
        "INSERT INTO wallet_tx (user_id, kind, amount_cents, note) VALUES (?,?,?,?)",
        (uid, "admin_adjust", amount, f"Admin: {note}"),
    )
    notify(uid, "Balance adjusted" if lang() == "en" else "تم تعديل الرصيد",
           f"{'+' if amount > 0 else ''}{fmt_money(amount)} — {note}",
           url_for("dashboard"), "wallet")
    flash(f"Adjusted by {fmt_money(amount)}.", "success")
    return redirect(url_for("admin_user_detail", uid=uid))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@login_required("admin")
def admin_delete_user(uid):
    if uid == current_user()["id"]:
        flash("You cannot delete yourself.", "error")
        return redirect(url_for("admin_user_detail", uid=uid))
    confirm = request.form.get("confirm_email", "").strip().lower()
    actual = db.query_one("SELECT email FROM users WHERE id=?", (uid,))
    if not actual or confirm != actual["email"]:
        flash("Email confirmation does not match.", "error")
        return redirect(url_for("admin_user_detail", uid=uid))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    flash(f"Deleted user {actual['email']}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/reset-password", methods=["POST"])
@login_required("admin")
def admin_reset_password(uid):
    new_pw = (request.form.get("new_password") or "").strip()
    if len(new_pw) < 6:
        flash("Password must be 6+ chars.", "error")
        return redirect(url_for("admin_user_detail", uid=uid))
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (generate_password_hash(new_pw), uid))
    notify(uid, "Password reset by admin" if lang() == "en" else "تم إعادة تعيين كلمة المرور",
           "Please log in with your new password.", url_for("login"), "lock")
    flash("Password reset.", "success")
    return redirect(url_for("admin_user_detail", uid=uid))


# ===== Super-admin: CAMPAIGN management =====

@app.route("/admin/campaigns/<int:cid>/status", methods=["POST"])
@login_required("admin")
def admin_campaign_status(cid):
    status = request.form.get("status", "")
    if status not in ("active", "paused", "ended", "draft"):
        abort(400)
    db.execute("UPDATE campaigns SET status=? WHERE id=?", (status, cid))
    flash(f"Campaign set to {status}.", "success")
    return redirect(request.referrer or url_for("admin_campaigns"))


@app.route("/admin/campaigns/<int:cid>/feature", methods=["POST"])
@login_required("admin")
def admin_campaign_feature(cid):
    c = db.query_one("SELECT featured FROM campaigns WHERE id=?", (cid,))
    if not c:
        abort(404)
    new_v = 0 if c["featured"] else 1
    db.execute("UPDATE campaigns SET featured=? WHERE id=?", (new_v, cid))
    flash("Featured." if new_v else "Unfeatured.", "success")
    return redirect(request.referrer or url_for("admin_campaigns"))


@app.route("/admin/campaigns/<int:cid>/delete", methods=["POST"])
@login_required("admin")
def admin_campaign_delete(cid):
    c = db.query_one("SELECT title, brand_id, spent_cents, budget_cents FROM campaigns WHERE id=?", (cid,))
    if not c:
        abort(404)
    refund = c["budget_cents"] - c["spent_cents"]
    if refund > 0:
        db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE id=?", (refund, c["brand_id"]))
        db.execute("INSERT INTO wallet_tx (user_id, kind, amount_cents, note) VALUES (?,?,?,?)",
                   (c["brand_id"], "refund", refund, f"Refund: campaign '{c['title']}' deleted by admin"))
    db.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    flash(f"Campaign deleted. {fmt_money(refund)} refunded.", "success")
    return redirect(url_for("admin_campaigns"))


# ===== Super-admin: SUBMISSION management =====

@app.route("/admin/submissions/<int:sid>/force-approve", methods=["POST"])
@login_required("admin")
def admin_force_approve(sid):
    s = db.query_one(
        """SELECT s.*, c.brand_id, c.payout_type, c.payout_rate_cents,
                  c.budget_cents, c.spent_cents, c.id AS cid
           FROM submissions s JOIN campaigns c ON c.id=s.campaign_id
           WHERE s.id=?""",
        (sid,),
    )
    if not s:
        abort(404)
    payout = parse_money_input(request.form.get("payout", ""))
    note = (request.form.get("note") or "Force-approved by admin").strip()
    if payout < 0:
        flash("Payout must be non-negative.", "error")
        return redirect(url_for("admin_submissions"))
    db.execute(
        "UPDATE submissions SET status='approved', earnings_cents=?, review_note=?, reviewed_at=datetime('now') WHERE id=?",
        (payout, note, sid),
    )
    if payout > 0:
        db.execute("UPDATE campaigns SET spent_cents = spent_cents + ? WHERE id=?", (payout, s["cid"]))
        db.execute("UPDATE users SET balance_cents = balance_cents + ?, total_paid_cents = total_paid_cents + ? WHERE id=?",
                   (payout, payout, s["creator_id"]))
        db.execute("INSERT INTO wallet_tx (user_id, kind, amount_cents, note) VALUES (?,?,?,?)",
                   (s["creator_id"], "earning", payout, note))
    notify(s["creator_id"], "Submission approved" if lang() == "en" else "تمت الموافقة",
           f"{fmt_money(payout)}", url_for("creator_submissions"), "check")
    flash(f"Force-approved. {fmt_money(payout)} credited.", "success")
    return redirect(url_for("admin_submissions"))


@app.route("/admin/submissions/<int:sid>/force-reject", methods=["POST"])
@login_required("admin")
def admin_force_reject(sid):
    s = db.query_one("SELECT creator_id FROM submissions s WHERE id=?", (sid,))
    if not s:
        abort(404)
    note = (request.form.get("note") or "Rejected by admin").strip()
    db.execute(
        "UPDATE submissions SET status='rejected', review_note=?, reviewed_at=datetime('now') WHERE id=?",
        (note, sid),
    )
    notify(s["creator_id"], "Submission rejected" if lang() == "en" else "تم رفض التقديم",
           note, url_for("creator_submissions"), "x")
    flash("Force-rejected.", "info")
    return redirect(url_for("admin_submissions"))


# ===== Super-admin: PAYOUT management =====

@app.route("/admin/payouts")
@login_required("admin")
def admin_payouts():
    status_f = request.args.get("status", "")
    sql = """SELECT p.*, u.name AS user_name, u.email AS user_email
             FROM payouts p JOIN users u ON u.id=p.user_id"""
    params: list = []
    if status_f in ("pending", "processing", "completed", "failed"):
        sql += " WHERE p.status=?"
        params.append(status_f)
    sql += " ORDER BY p.created_at DESC LIMIT 200"
    rows = db.query(sql, tuple(params))
    return render_template("admin/payouts.html", rows=rows, status_f=status_f)


@app.route("/admin/payouts/<int:pid>/status", methods=["POST"])
@login_required("admin")
def admin_payout_status(pid):
    status = request.form.get("status", "")
    reference = (request.form.get("reference") or "").strip()
    if status not in ("pending", "processing", "completed", "failed"):
        abort(400)
    p = db.query_one("SELECT user_id, amount_cents, status FROM payouts WHERE id=?", (pid,))
    if not p:
        abort(404)
    db.execute("UPDATE payouts SET status=?, reference=? WHERE id=?", (status, reference, pid))
    # If admin marks failed and previous was pending/processing: refund the user's balance
    if status == "failed" and p["status"] in ("pending", "processing"):
        db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE id=?",
                   (p["amount_cents"], p["user_id"]))
        db.execute("INSERT INTO wallet_tx (user_id, kind, amount_cents, note, ref_id) VALUES (?,?,?,?,?)",
                   (p["user_id"], "refund", p["amount_cents"], "Withdrawal failed — refunded by admin", pid))
    status_ar = {"pending": "قيد الانتظار", "processing": "قيد المعالجة",
                 "completed": "اكتمل", "failed": "فشل"}.get(status, status)
    notify(p["user_id"],
           (f"Withdrawal {status}" if lang() == "en" else f"حالة السحب: {status_ar}"),
           (reference or fmt_money(p['amount_cents'])),
           url_for("creator_wallet"), "wallet", email=True)
    flash(f"Payout marked {status}.", "success")
    return redirect(url_for("admin_payouts"))


# ============================================================
# Error handlers
# ============================================================

# ============================================================
# Fan amplification network — share links + leaderboards
# ============================================================

def _visitor_token() -> str:
    """Cookie-based pseudo-identity for anonymous click dedup."""
    tok = request.cookies.get("vtok")
    if not tok:
        tok = secrets.token_urlsafe(12)
    return tok


def _short_ua(s: str | None) -> str:
    return (s or "")[:240]


@app.route("/v/<token>")
def view_share(token):
    """Public share link: count the click then redirect to the actual video URL.

    - If a fan is logged in, the click is credited to them.
    - Otherwise it's tracked via a long-lived `vtok` cookie (anonymous identity).
    - Dedup window: 1 click per (submission, fan/visitor) per 24h.
    """
    s = db.query_one(
        "SELECT id, video_url, creator_id FROM submissions WHERE share_token=?", (token,)
    )
    if not s:
        abort(404)

    u = current_user()
    fan_id = u["id"] if (u and u["role"] == "fan") else None
    vtok = _visitor_token()

    # Dedup: same fan_id OR same vtok against same submission within 24h
    if fan_id:
        existing = db.query_one(
            """SELECT 1 FROM view_clicks
               WHERE submission_id=? AND fan_id=?
                 AND created_at > datetime('now','-1 day') LIMIT 1""",
            (s["id"], fan_id),
        )
    else:
        existing = db.query_one(
            """SELECT 1 FROM view_clicks
               WHERE submission_id=? AND visitor_token=? AND fan_id IS NULL
                 AND created_at > datetime('now','-1 day') LIMIT 1""",
            (s["id"], vtok),
        )
    if not existing:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        db.execute(
            """INSERT INTO view_clicks (submission_id, fan_id, visitor_token, ip, ua)
               VALUES (?,?,?,?,?)""",
            (s["id"], fan_id, vtok, ip, _short_ua(request.headers.get("User-Agent"))),
        )

    resp = redirect(s["video_url"], code=302)
    # 1-year cookie for anonymous identity continuity
    resp.set_cookie("vtok", vtok, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax")
    return resp


# ----- Fan auth -----

@app.route("/fan/register", methods=["GET", "POST"])
def fan_register():
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        if rate_limited(f"fanreg:ip:{ip}", max_calls=5, window_seconds=3600):
            flash("Too many registrations from this address.", "error")
            return render_template("fan/register.html"), 429
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not name or not email or len(password) < 6:
            flash("Name, email, and 6+ char password are required.", "error")
            return render_template("fan/register.html", name=name, email=email)
        if db.query_one("SELECT 1 FROM users WHERE email=?", (email,)):
            flash(t("auth_email_taken", lang()), "error")
            return render_template("fan/register.html", name=name, email=email)
        verify_token = secrets.token_urlsafe(24)
        uid = db.execute(
            """INSERT INTO users
               (email,password_hash,name,role,lang,email_verified,email_verification_token)
               VALUES (?,?,?,?,?,0,?)""",
            (email, generate_password_hash(password), name, "fan", lang(), verify_token),
        )
        # Link previous anonymous clicks (same vtok) to this new fan account
        vtok = request.cookies.get("vtok")
        if vtok:
            db.execute(
                "UPDATE view_clicks SET fan_id=? WHERE fan_id IS NULL AND visitor_token=?",
                (uid, vtok),
            )
        # Send verification email (no-op if SMTP unconfigured — see mailer.py)
        verify_url = url_for("fan_verify", token=verify_token, _external=True)
        subject, html = mailer.render_verification_email(name, verify_url, lang())
        sent = mailer.send_email(email, subject, html)
        # Do NOT auto-login — user must verify first
        return render_template("fan/check_email.html",
                               email=email, smtp_ok=sent, verify_url=verify_url)
    return render_template("fan/register.html")


@app.route("/fan/verify/<token>")
def fan_verify(token):
    row = db.query_one(
        "SELECT id, name, email_verified FROM users WHERE email_verification_token=? AND role='fan'",
        (token,),
    )
    if not row:
        return render_template("fan/verify_result.html", success=False), 404
    if not row["email_verified"]:
        db.execute(
            "UPDATE users SET email_verified=1, email_verification_token=NULL WHERE id=?",
            (row["id"],),
        )
    # Auto-login the fan now that email is verified
    session.clear()
    session["user_id"] = row["id"]
    return render_template("fan/verify_result.html", success=True, name=row["name"])


@app.route("/fan/resend-verification", methods=["POST"])
def fan_resend_verification():
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return redirect(url_for("fan_register"))
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    if rate_limited(f"resend:ip:{ip}", max_calls=5, window_seconds=3600) or \
       rate_limited(f"resend:email:{email}", max_calls=3, window_seconds=3600):
        flash("Too many resend requests.", "error")
        return redirect(url_for("fan_login"))
    row = db.query_one(
        "SELECT id, name, lang, email_verified FROM users WHERE email=? AND role='fan'",
        (email,),
    )
    if row and not row["email_verified"]:
        new_token = secrets.token_urlsafe(24)
        db.execute("UPDATE users SET email_verification_token=? WHERE id=?", (new_token, row["id"]))
        verify_url = url_for("fan_verify", token=new_token, _external=True)
        subject, html = mailer.render_verification_email(row["name"], verify_url, row["lang"] or lang())
        mailer.send_email(email, subject, html)
    flash("If the email exists and is unverified, a new link was sent.", "success")
    return redirect(url_for("fan_login"))


@app.route("/fan/login", methods=["GET", "POST"])
def fan_login():
    """Same auth as /login but UI is fan-themed and lands on /fan."""
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        email = (request.form.get("email") or "").strip().lower()
        if rate_limited(f"login:ip:{ip}", max_calls=10, window_seconds=300):
            flash("Too many attempts.", "error")
            return render_template("fan/login.html", email=email), 429
        password = request.form.get("password") or ""
        row = db.query_one("SELECT * FROM users WHERE email=? AND role='fan'", (email,))
        if not row or not check_password_hash(row["password_hash"], password):
            flash(t("auth_invalid", lang()), "error")
            return render_template("fan/login.html", email=email)
        if row["banned"]:
            flash("Account suspended.", "error")
            return render_template("fan/login.html", email=email)
        if not row["email_verified"]:
            return render_template("fan/check_email.html", email=email, smtp_ok=True, need_verify=True)
        session.clear()
        session["user_id"] = row["id"]
        return redirect(safe_next(request.args.get("next")) or url_for("fan_dashboard"))
    return render_template("fan/login.html")


@app.route("/fan")
@login_required("fan")
def fan_dashboard():
    u = current_user()
    stats = db.query_one(
        "SELECT COUNT(*) AS clicks, COUNT(DISTINCT submission_id) AS videos FROM view_clicks WHERE fan_id=?",
        (u["id"],),
    )
    top_creators = db.query(
        """SELECT cr.id, cr.name, COUNT(*) AS clicks
           FROM view_clicks v
           JOIN submissions s ON s.id=v.submission_id
           JOIN users cr ON cr.id=s.creator_id
           WHERE v.fan_id=?
           GROUP BY cr.id ORDER BY clicks DESC LIMIT 10""",
        (u["id"],),
    )
    return render_template("fan/dashboard.html", stats=stats, top_creators=top_creators)


# ----- Leaderboards -----

def _leaderboard_for_creator(creator_id: int, limit: int = 50):
    return db.query(
        """SELECT u.id, u.name, COUNT(*) AS clicks
           FROM view_clicks v
           JOIN submissions s ON s.id=v.submission_id
           LEFT JOIN users u ON u.id=v.fan_id
           WHERE s.creator_id=? AND v.fan_id IS NOT NULL
           GROUP BY u.id ORDER BY clicks DESC LIMIT ?""",
        (creator_id, limit),
    )


@app.route("/leaderboard")
def leaderboard_global():
    """Public global leaderboard: top creators by total tracked clicks."""
    top_creators = db.query(
        """SELECT cr.id, cr.name, COUNT(*) AS clicks,
                  COUNT(DISTINCT v.fan_id) AS unique_fans
           FROM view_clicks v
           JOIN submissions s ON s.id=v.submission_id
           JOIN users cr ON cr.id=s.creator_id
           WHERE v.created_at > datetime('now','-30 day')
           GROUP BY cr.id ORDER BY clicks DESC LIMIT 50"""
    )
    return render_template("leaderboard/global.html", rows=top_creators)


@app.route("/leaderboard/creator/<int:creator_id>")
def leaderboard_creator(creator_id):
    creator = db.query_one("SELECT id, name, avatar_url FROM users WHERE id=? AND role='creator'", (creator_id,))
    if not creator:
        abort(404)
    top_fans = _leaderboard_for_creator(creator_id, 50)
    total = db.query_one(
        """SELECT COUNT(*) AS c, COUNT(DISTINCT v.fan_id) AS f
           FROM view_clicks v
           JOIN submissions s ON s.id=v.submission_id
           WHERE s.creator_id=?""",
        (creator_id,),
    )
    return render_template("leaderboard/creator.html",
                           creator=creator, rows=top_fans, total=total)


@app.route("/leaderboard/submission/<int:sid>")
def leaderboard_submission(sid):
    s = db.query_one(
        """SELECT s.id, s.video_url, s.platform, s.share_token,
                  cr.id AS creator_id, cr.name AS creator_name
           FROM submissions s JOIN users cr ON cr.id=s.creator_id
           WHERE s.id=?""",
        (sid,),
    )
    if not s:
        abort(404)
    top_fans = db.query(
        """SELECT u.id, u.name, COUNT(*) AS clicks
           FROM view_clicks v
           LEFT JOIN users u ON u.id=v.fan_id
           WHERE v.submission_id=? AND v.fan_id IS NOT NULL
           GROUP BY u.id ORDER BY clicks DESC LIMIT 50""",
        (sid,),
    )
    total = db.query_one(
        "SELECT COUNT(*) AS c, COUNT(DISTINCT visitor_token) AS uniq FROM view_clicks WHERE submission_id=?",
        (sid,),
    )
    return render_template("leaderboard/submission.html",
                           s=s, rows=top_fans, total=total)


# ----- Creator: share + amplification stats -----

@app.route("/creator/submissions/<int:sid>/amplify")
@login_required("creator")
def creator_amplify(sid):
    u = current_user()
    s = db.query_one(
        """SELECT s.*, c.title AS campaign_title FROM submissions s
           JOIN campaigns c ON c.id=s.campaign_id
           WHERE s.id=? AND s.creator_id=?""",
        (sid, u["id"]),
    )
    if not s:
        abort(404)
    total = db.query_one(
        """SELECT COUNT(*) AS c,
                  COUNT(DISTINCT visitor_token) AS uniq,
                  COUNT(DISTINCT fan_id) AS named_fans
           FROM view_clicks WHERE submission_id=?""",
        (sid,),
    )
    top_fans = db.query(
        """SELECT u.name, COUNT(*) AS clicks
           FROM view_clicks v JOIN users u ON u.id=v.fan_id
           WHERE v.submission_id=?
           GROUP BY u.id ORDER BY clicks DESC LIMIT 10""",
        (sid,),
    )
    share_url = url_for("view_share", token=s["share_token"], _external=True)
    return render_template("creator/amplify.html",
                           s=s, total=total, top_fans=top_fans, share_url=share_url)


@app.route("/healthz")
def healthz():
    """Lightweight health probe for uptime monitors. Checks DB connectivity."""
    try:
        db.query_one("SELECT 1")
        return jsonify(status="ok"), 200
    except Exception as e:
        return jsonify(status="error", detail=str(e)[:120]), 503


@app.errorhandler(404)
def e404(_):
    return render_template("error.html", code=404, msg="Not found"), 404


@app.errorhandler(403)
def e403(_):
    return render_template("error.html", code=403, msg="Forbidden"), 403


# ============================================================
# CLI: init-db and seed
# ============================================================

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"
    if arg == "init":
        db.init_db()
        print("DB initialized.")
        sys.exit(0)
    if arg == "seed":
        import seed
        seed.run()
        sys.exit(0)
    # Ensure DB exists + apply additive migrations
    if not db.DB_PATH.exists():
        db.init_db()
        print("DB created. Run `python app.py seed` for sample data.")
    db.migrate()
    # Production: never use Flask's dev server. The WSGI entry point
    # (passenger_wsgi.py or `gunicorn app:app`) handles serving.
    host = os.environ.get("CLIPSCASH_HOST", "127.0.0.1")
    port = int(os.environ.get("CLIPSCASH_PORT", "5001"))
    app.run(host=host, port=port, debug=not IS_PROD)


# Run migrations on import (so WSGI entry points like passenger_wsgi
# automatically apply schema changes on deploy).
try:
    if not db.DB_PATH.exists():
        db.init_db()
    db.migrate()
except Exception as _e:
    print(f"Warning: migration on import failed: {_e}")
