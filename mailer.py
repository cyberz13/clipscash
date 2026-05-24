"""SMTP email helper.

Reads config from env (set in /etc/clipscash.env):
  SMTP_HOST       — e.g. smtp.hostinger.com
  SMTP_PORT       — usually 465 (SSL) or 587 (STARTTLS)
  SMTP_USER       — e.g. noreply@clipscash.app
  SMTP_PASS       — mailbox password
  SMTP_FROM_NAME  — display name, e.g. "Clipscash"

If SMTP_HOST is unset, send_email() is a no-op that just logs to stdout,
so the app keeps working in dev or before SMTP is configured.
"""
from __future__ import annotations
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("mailer")


def is_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"))


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Returns True on success, False on failure (including unconfigured)."""
    if not is_configured():
        log.warning("SMTP not configured — would have sent to %s subject=%r", to, subject)
        print(f"[mailer:unconfigured] to={to} subject={subject!r}")
        return False

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ.get("SMTP_PASS", "")
    from_name = os.environ.get("SMTP_FROM_NAME", "Clipscash")

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or _html_to_text(html))
    msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(user, pwd)
                s.send_message(msg)
        log.info("email sent to=%s subject=%r", to, subject)
        return True
    except Exception as e:
        log.exception("email send failed to=%s: %s", to, e)
        print(f"[mailer:error] to={to} err={e!r}")
        return False


def _html_to_text(html: str) -> str:
    """Crude HTML→text fallback so the multipart message has both parts."""
    import re
    s = re.sub(r"<[^>]+>", "", html)
    s = re.sub(r"\s+\n", "\n", s)
    return s.strip()


# ---------- email templates ----------

def render_verification_email(name: str, link: str, lang: str = "ar") -> tuple[str, str]:
    """Returns (subject, html_body)."""
    if lang == "ar":
        subject = "أكّد بريدك الإلكتروني — كليبس كاش"
        html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><body style="font-family:Tahoma,Arial,sans-serif;background:#0b0b0b;color:#ece4d3;padding:32px;margin:0;">
  <div style="max-width:520px;margin:0 auto;background:#181818;border:1px solid #262626;border-radius:14px;padding:32px;">
    <div style="font-weight:800;font-size:24px;letter-spacing:-1px;margin-bottom:24px;">
      CLIPS<span style="color:#c8ff5e">CASH</span>
    </div>
    <h2 style="color:#ece4d3;margin:0 0 12px;">مرحباً {name}،</h2>
    <p style="color:#9a9388;line-height:1.6;">
      شكراً لإنشاء حسابك كمعجب في كليبس كاش. اضغط الزر أدناه لتأكيد بريدك وتفعيل حسابك:
    </p>
    <div style="text-align:center;margin:28px 0;">
      <a href="{link}" style="display:inline-block;background:#c8ff5e;color:#0c1500;
         padding:12px 28px;border-radius:999px;font-weight:700;text-decoration:none;">
        تأكيد البريد
      </a>
    </div>
    <p style="color:#9a9388;font-size:13px;line-height:1.6;">
      أو انسخ الرابط التالي:<br>
      <span style="word-break:break-all;color:#c8ff5e;">{link}</span>
    </p>
    <p style="color:#9a9388;font-size:12px;border-top:1px solid #262626;padding-top:16px;margin-top:24px;">
      إذا لم تنشئ هذا الحساب، تجاهل هذه الرسالة. الرابط ينتهي خلال ٢٤ ساعة.
    </p>
  </div>
</body></html>"""
    else:
        subject = "Confirm your email — Clipscash"
        html = f"""<!DOCTYPE html>
<html lang="en"><body style="font-family:Arial,sans-serif;background:#0b0b0b;color:#ece4d3;padding:32px;margin:0;">
  <div style="max-width:520px;margin:0 auto;background:#181818;border:1px solid #262626;border-radius:14px;padding:32px;">
    <div style="font-weight:800;font-size:24px;letter-spacing:-1px;margin-bottom:24px;">
      CLIPS<span style="color:#c8ff5e">CASH</span>
    </div>
    <h2 style="color:#ece4d3;margin:0 0 12px;">Hi {name},</h2>
    <p style="color:#9a9388;line-height:1.6;">
      Thanks for joining Clipscash as a fan. Click the button below to confirm your email and activate your account:
    </p>
    <div style="text-align:center;margin:28px 0;">
      <a href="{link}" style="display:inline-block;background:#c8ff5e;color:#0c1500;
         padding:12px 28px;border-radius:999px;font-weight:700;text-decoration:none;">
        Confirm email
      </a>
    </div>
    <p style="color:#9a9388;font-size:13px;line-height:1.6;">
      Or copy this link:<br>
      <span style="word-break:break-all;color:#c8ff5e;">{link}</span>
    </p>
    <p style="color:#9a9388;font-size:12px;border-top:1px solid #262626;padding-top:16px;margin-top:24px;">
      If you didn't create this account, ignore this message. The link expires in 24 hours.
    </p>
  </div>
</body></html>"""
    return subject, html
