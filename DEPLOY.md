# دليل نشر Clipscash على Hostinger

> منصة B2B لإدارة برامج المبدعين — Flask + SQLite.
> هذا الدليل يستخدم **Hostinger Cloud Hosting** عبر hPanel + Python App.

---

## 1) قبل النشر — تأكد عندك:

- [ ] حساب Hostinger Cloud Hosting (أي خطة فيها "Python" مفعّل)
- [ ] الدومين مربوط بحساب Hostinger أو nameservers موجّهة لـHostinger
- [ ] FTP أو File Manager للوصول للملفات
- [ ] الكود نظيف (شغّل `python app.py` محلياً وتأكد كل شيء سليم)

---

## 2) جهّز ملف `.env` (لا ترفعه لـgit)

ولّد `SECRET_KEY` قوي:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

ثم خذ نسخة من `.env.example` وعدّل القيم:

```env
CLIPSCASH_SECRET=هنا_64_حرف_من_السكربت_فوق
CLIPSCASH_ENV=prod
CLIPSCASH_BEHIND_PROXY=1
CLIPSCASH_HTTPS=1
```

---

## 3) رفع الملفات

### الطريقة (أ) — File Manager في hPanel:
1. ادخل **hPanel → Files → File Manager**
2. ادخل لمجلد `public_html` (أو مجلد خاص لو عندك أكثر من موقع)
3. ارفع كل محتويات `C:\Users\WinDows\clipscash` (عدا `clipscash.db` و `.secret_key` و `__pycache__`)
4. تأكد أن هذه موجودة:
   - `passenger_wsgi.py`
   - `app.py`
   - `requirements.txt`
   - `static/`, `templates/`, `db.py`, `i18n.py`, `fraud.py`, `ai_insights.py`, `schema.sql`

### الطريقة (ب) — Git (أسرع وأنظف):
1. أنشئ ريبو خاص على GitHub
2. على Hostinger: **hPanel → Advanced → Git** → Create New Repository
3. اربط الريبو + اختر branch + path للنشر

---

## 4) إعداد Python App في hPanel

1. ادخل **hPanel → Advanced → Python**
2. اضغط **Create Application**
3. عبّئ:
   - **Python version**: 3.11 أو 3.12 (أحدث المتاح)
   - **Application URL**: اختر الدومين (مثلاً `clipscash.co`)
   - **Application root**: نفس المسار اللي رفعت فيه الملفات (مثلاً `public_html` أو `apps/clipscash`)
   - **Application startup file**: `passenger_wsgi.py`
   - **Application entry point**: `application`
4. اضغط **Create**

---

## 5) ثبّت الحزم

في صفحة الـPython App اللي أنشأتها:

1. تحت "Configuration files" → اضغط أيقونة المحطة الطرفية (Terminal)
2. اكتب:

```bash
source /home/USER/virtualenv/PATH/3.X/bin/activate  # الأمر الجاهز فوق المحطة
pip install -r requirements.txt
```

أو من نفس صفحة Python App: قسم **"Detect configuration files"** → سيُثبتها تلقائياً من `requirements.txt`.

---

## 6) أضف متغيرات البيئة

في نفس صفحة الـPython App → قسم **Environment variables**:

| Name | Value |
|------|-------|
| `CLIPSCASH_SECRET` | (السكربت من خطوة 2) |
| `CLIPSCASH_ENV` | `prod` |
| `CLIPSCASH_BEHIND_PROXY` | `1` |
| `CLIPSCASH_HTTPS` | `1` |

اضغط **Save**.

---

## 7) أنشئ قاعدة البيانات الأولى + الأدمن

في الـTerminal من Python App:

```bash
python app.py init       # ينشئ clipscash.db فاضي
python app.py seed       # (اختياري) بيانات تجريبية + admin@clipscash.local / password
```

**مهم**: غيّر كلمة سر الأدمن فوراً عبر `/admin/users/<id>/reset-password` بعد أول تسجيل دخول.

---

## 8) فعّل التطبيق

في صفحة الـPython App اضغط **Restart**.
انتظر 10-30 ثانية، ثم افتح دومينك في المتصفح.

من المفروض ترى صفحة `/login`. ادخل بـ`admin@clipscash.local / password` ← بعدها غيّر السر.

---

## 9) فعّل SSL (HTTPS)

1. **hPanel → Security → SSL**
2. اختر الدومين → **Install** (Let's Encrypt مجاني)
3. فعّل **Force HTTPS**

بعدها كل الترافيك سيكون https تلقائياً، والـCookies ستحمل علامة Secure تلقائياً.

---

## 10) DNS للدومين

إذا الدومين مسجَّل في مكان آخر (Namecheap/SaudiNIC...) عيّن Nameservers:

```
ns1.dns-parking.com
ns2.dns-parking.com
```

(أو حسب ما تظهره Hostinger في **hPanel → Domains → Manage**)

أو إذا تفضّل تخلي Nameservers خارج Hostinger:
- A record للدومين → IP الشيرد هوستينج (موجود في hPanel)
- CNAME للـwww → الدومين الأساسي

التغيير ياخذ من ساعة إلى 24 ساعة للانتشار.

---

## 11) (موصى به) Cloudflare في المقدمة

ضع Cloudflare قبل Hostinger للحصول على:
- CDN + Cache (سرعة أعلى)
- WAF + DDoS مجاناً
- TLS 1.3 و HTTP/3
- إخفاء IP السيرفر الحقيقي

الإعداد:
1. أضف الدومين في Cloudflare → اختر Free plan
2. Cloudflare يفحص DNS تلقائياً
3. غيّر Nameservers في مسجل الدومين لـCloudflare
4. SSL/TLS mode في Cloudflare: **Full (strict)**

---

## 12) تحديثات بعد النشر

بعد كل تعديل تسوّيه:

```bash
# على جهازك:
git add . && git commit -m "Update" && git push

# في hPanel Python App:
# 1. اضغط "Pull Latest" إذا تستعمل git integration
# 2. اضغط "Restart" دائماً
```

أو إن كنت ترفع يدوياً عبر File Manager، فقط ارفع الملفات ثم اضغط **Restart**.

---

## 13) النسخ الاحتياطي (backup)

**ضروري**: انسخ هذه الملفات يومياً:
- `clipscash.db` — قاعدة البيانات (كل شيء)
- `static/uploads/` — صور الإثبات وأفاتار المستخدمين
- `.secret_key` — يجب أن يبقى ثابتاً (وإلا تموت الجلسات)

في Hostinger: **hPanel → Files → Backups**

أو ارفع `clipscash.db` كل يوم تلقائياً عبر cron:

```bash
0 3 * * * cp /home/USER/path/to/clipscash.db /home/USER/backups/clipscash_$(date +\%Y\%m\%d).db
```

---

## 14) Troubleshooting

### "500 Internal Server Error"
- شوف اللوقات: **hPanel → Python → View Log**
- 90% الأخطاء: مكتبة ناقصة (شغّل `pip install -r requirements.txt` مرة ثانية)

### "Session expired" متكرر
- `CLIPSCASH_SECRET` لازم يكون نفسه عبر كل restart. تأكد إنه متغير بيئة وليس عشوائي.

### ملف `.secret_key` تم رفعه بالخطأ
- احذفه واستبدله بمتغير بيئة `CLIPSCASH_SECRET`. لا ترفع هذا الملف لـgit أبداً.

### الصور المرفوعة تختفي بعد تحديث
- في Hostinger Shared Hosting: `static/uploads/` يبقى. لكن في PaaS مثل Render فالقرص ephemeral — استخدم Cloudinary/S3.

### الموقع بطيء
- فعّل Cloudflare (خطوة 11)
- في Python App زد عدد workers: في `passenger_wsgi.py` أو متغير `WEB_CONCURRENCY=3`

---

## 15) قائمة فحص نهائية ✅

- [ ] `/login` يفتح ويعرض النموذج
- [ ] `/welcome` يفتح ويعرض الـlanding التسويقية
- [ ] دخول `admin@...` ينجح، وغيّرت السر
- [ ] HTTPS مفعّل والقفل أخضر في المتصفح
- [ ] أنشأت أول براند تجريبي من `/admin/brands/new`
- [ ] البراند سجل دخول وأنشأ مبدع
- [ ] المبدع سجل دخول ورأى براندَه
- [ ] طلب Demo من `/welcome` ظهر في `/admin/demos`
- [ ] أعددت backup يومي

---

## 16) خطوات اختيارية

- **Email**: ربط SMTP لإرسال إشعارات (Resend / SendGrid / Hostinger Mail)
- **Domain custom email**: `support@clipscash.co` عبر Hostinger Mail (مجاني مع الاستضافة)
- **Analytics**: Plausible أو Umami (خصوصية أعلى من GA)
- **Status page**: Better Uptime مجاني لـ10 monitors

---

تم. منصتك في الإنتاج. 🚀
