"""Seed sample data into Clipscash DB."""
from __future__ import annotations
import json
import random
from werkzeug.security import generate_password_hash
import db

CAMPAIGNS = [
    ("Nova Energy Drink", "Nova Beverages", "food",
     "Quick edits showcasing Nova's launch flavors — burst-of-energy clips welcome.",
     "Show the can, take a sip, do something energetic. 15–30s. Hashtags: #NovaFuel #Energized.",
     "per_view", 250, 500_00, "tiktok,reels,shorts"),
    ("Pixel Studios mobile game review", "Pixel Studios", "gaming",
     "Honest 60-second review of our latest puzzle game.",
     "Record 30s of gameplay, give a quick verdict. Mention level 8 trick. Hashtags: #PixelPuzzles.",
     "per_post", 25000, 750_00, "tiktok,shorts"),
    ("Aria Beauty GRWM", "Aria Cosmetics", "beauty",
     "Get-Ready-With-Me using our new lip stain.",
     "GRWM video, full-face, focus on the lip application moment. #AriaGlow",
     "per_view", 300, 1000_00, "tiktok,reels"),
    ("FitForge workout challenge", "FitForge", "fitness",
     "30-day challenge content with $5K prize pool for top creators.",
     "Daily 20s clip from challenge, tag @fitforge. Use #FitForge30.",
     "per_engagement", 5, 500_00, "reels,shorts,x"),
    ("Hype Threads streetwear", "Hype Threads", "fashion",
     "Outfit-of-the-day with our new spring drop.",
     "OOTD reel/short, showcase 2+ pieces. Tag @hypethreads.",
     "per_engagement", 5, 600_00, "reels,tiktok"),
    ("Lumi Skincare review", "Lumi Skincare", "beauty",
     "Honest 45-day skincare review.",
     "Before/after style. Genuine reviews only. #LumiGlow",
     "per_post", 40000, 1200_00, "tiktok,reels,shorts"),
    ("Voltride e-bike content", "Voltride", "tech",
     "Showcase the Voltride X1 in urban settings.",
     "Show the bike, ride shot, a call-out of one feature. #VoltrideX1",
     "per_view", 400, 2000_00, "shorts,tiktok"),
    ("Crisp & Co. recipe shorts", "Crisp & Co.", "food",
     "30-second recipes featuring our snack chips.",
     "Quick recipe, finish with chip product. #CrispRecipes",
     "per_view", 200, 300_00, "reels,shorts"),
    ("OrbitTalks podcast clips", "Orbit Media", "tech",
     "Cut highlight clips from our podcast.",
     "Pick 45–60s standout moment, add caption. #OrbitTalks",
     "per_post", 15000, 450_00, "x,shorts,reels"),
]

CREATOR_NAMES = [
    ("Layla Hassan", "layla@clip.test"),
    ("Omar Khalifa", "omar@clip.test"),
    ("Maya Stone", "maya@clip.test"),
    ("Jin Park", "jin@clip.test"),
    ("Sara Mansour", "sara@clip.test"),
    ("Diego Rivas", "diego@clip.test"),
    ("Noor Aziz", "noor@clip.test"),
    ("Tess Wong", "tess@clip.test"),
]


def run():
    db.init_db()
    conn = db.sqlite3.connect(db.DB_PATH)
    conn.row_factory = db.sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    cur.execute("DELETE FROM wallet_tx")
    cur.execute("DELETE FROM payouts")
    cur.execute("DELETE FROM trust_marks")
    cur.execute("DELETE FROM notifications")
    cur.execute("DELETE FROM submissions")
    cur.execute("DELETE FROM campaigns")
    cur.execute("DELETE FROM users")

    pw = generate_password_hash("password")

    cur.execute(
        "INSERT INTO users (email,password_hash,name,role,lang) VALUES (?,?,?,?,?)",
        ("admin@clipscash.local", pw, "Admin", "admin", "ar"),
    )

    brand_ids = []
    for title, brand_name, *_ in CAMPAIGNS:
        email = f"{brand_name.lower().replace(' & ', '').replace(' ', '').replace('.', '')}@brand.test"
        cur.execute(
            "INSERT INTO users (email,password_hash,name,role,balance_cents,lang) VALUES (?,?,?,?,?,?)",
            (email, pw, brand_name, "brand", 50_000_00, "en"),
        )
        brand_ids.append(cur.lastrowid)

    creator_ids = []
    for name, email in CREATOR_NAMES:
        socials = json.dumps({
            "tiktok": "@" + email.split("@")[0],
            "instagram": "@" + email.split("@")[0],
            "youtube": "",
            "x": "@" + email.split("@")[0],
        })
        cur.execute(
            "INSERT INTO users (email,password_hash,name,role,socials,country,lang) VALUES (?,?,?,?,?,?,?)",
            (email, pw, name, "creator", socials, random.choice(["AE","SA","EG","US","UK","JO"]), random.choice(["ar","en"])),
        )
        creator_ids.append(cur.lastrowid)

    campaign_ids = []
    for i, (title, brand_name, cat, desc, brief, ptype, rate, budget, plats) in enumerate(CAMPAIGNS):
        cur.execute(
            """INSERT INTO campaigns
               (brand_id,title,brand_name,description,brief,category,platforms,
                payout_type,payout_rate_cents,budget_cents,image_url,status,featured)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (brand_ids[i], title, brand_name, desc, brief, cat, plats,
             ptype, rate, budget, f"/static/img/camp{i+1}.svg", "active",
             1 if i < 3 else 0),
        )
        campaign_ids.append(cur.lastrowid)

    # Sample submissions
    sample_urls = {
        "tiktok": "https://www.tiktok.com/@demo/video/7300000000000000000",
        "reels": "https://www.instagram.com/reel/Cabcdefghij/",
        "shorts": "https://www.youtube.com/shorts/abcd1234XYZ",
        "x": "https://x.com/demo/status/1700000000000000000",
    }
    for _ in range(40):
        cid = random.choice(campaign_ids)
        crid = random.choice(creator_ids)
        camp = cur.execute("SELECT payout_type, payout_rate_cents, platforms FROM campaigns WHERE id=?", (cid,)).fetchone()
        plat = random.choice(camp["platforms"].split(","))
        url = sample_urls.get(plat, sample_urls["tiktok"])
        views = random.randint(500, 200_000)
        likes = int(views * random.uniform(0.02, 0.08))
        comments = int(views * random.uniform(0.001, 0.01))
        status = random.choices(["pending","approved","paid","rejected"], weights=[30,30,30,10])[0]
        verified_views = int(views * random.uniform(0.6, 1.0)) if status in ("approved","paid") else 0
        verified_likes = int(likes * random.uniform(0.6, 1.0)) if status in ("approved","paid") else 0
        verified_comments = int(comments * random.uniform(0.6, 1.0)) if status in ("approved","paid") else 0
        earnings = 0
        if status in ("approved","paid"):
            if camp["payout_type"] == "per_view":
                earnings = int(verified_views / 1000.0 * camp["payout_rate_cents"])
            elif camp["payout_type"] == "per_post":
                earnings = camp["payout_rate_cents"]
            elif camp["payout_type"] == "per_engagement":
                earnings = (verified_likes + verified_comments) * camp["payout_rate_cents"]
        fraud_score = random.randint(0, 80)
        cur.execute(
            """INSERT INTO submissions
               (campaign_id,creator_id,video_url,platform,self_views,self_likes,self_comments,
                verified_views,verified_likes,verified_comments,earnings_cents,status,fraud_score,
                review_note,reviewed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, crid, url, plat, views, likes, comments,
             verified_views, verified_likes, verified_comments, earnings,
             status, fraud_score,
             "Great work!" if status in ("approved","paid") else ("Off-brief" if status == "rejected" else None),
             None),
        )
        if status in ("approved","paid"):
            cur.execute("UPDATE users SET balance_cents = balance_cents + ?, total_paid_cents = total_paid_cents + ? WHERE id=?",
                        (earnings, earnings, crid))
            cur.execute("UPDATE campaigns SET spent_cents = spent_cents + ? WHERE id=?", (earnings, cid))
            cur.execute("INSERT INTO wallet_tx (user_id,kind,amount_cents,note) VALUES (?,?,?,?)",
                        (crid, "earning", earnings, "Approved submission"))

    # Demo accounts hint
    print("Seed complete.")
    print("Login (any role) with password: password")
    print("  admin@clipscash.local")
    print("  novabeverages@brand.test   (brand)")
    print("  layla@clip.test            (creator)")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
