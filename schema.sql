-- Clipscash database schema (full MVP)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('creator','brand','admin','fan')),
    avatar_url      TEXT,
    bio             TEXT,
    country         TEXT,
    socials         TEXT,                              -- JSON: {tiktok, instagram, youtube, x}
    balance_cents   INTEGER NOT NULL DEFAULT 0,        -- available balance
    pending_cents   INTEGER NOT NULL DEFAULT 0,        -- pending (approved but not paid)
    total_paid_cents INTEGER NOT NULL DEFAULT 0,       -- lifetime earnings
    payout_method   TEXT,                              -- paypal | wise | usdt
    payout_details  TEXT,
    lang            TEXT NOT NULL DEFAULT 'ar',
    banned          INTEGER NOT NULL DEFAULT 0,
    banned_reason   TEXT,
    banned_at       TEXT,
    brand_id        INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- creator → brand owner; NULL for brand/admin
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    brand_name      TEXT NOT NULL,
    description     TEXT NOT NULL,
    brief           TEXT NOT NULL,
    category        TEXT NOT NULL,
    platforms       TEXT NOT NULL,                     -- comma list
    hashtags        TEXT,
    mentions        TEXT,
    min_duration    INTEGER DEFAULT 15,                -- seconds
    max_duration    INTEGER DEFAULT 90,
    example_links   TEXT,
    payout_type     TEXT NOT NULL CHECK (payout_type IN ('per_view','per_post','per_engagement','hybrid')),
    payout_rate_cents INTEGER NOT NULL DEFAULT 0,
    budget_cents    INTEGER NOT NULL DEFAULT 0,
    spent_cents     INTEGER NOT NULL DEFAULT 0,
    min_payout_cents INTEGER NOT NULL DEFAULT 100,
    image_url       TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','ended','draft')),
    featured        INTEGER NOT NULL DEFAULT 0,
    starts_at       TEXT,
    ends_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    creator_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    video_url       TEXT NOT NULL,
    platform        TEXT NOT NULL,
    caption         TEXT,
    proof_url_1     TEXT,
    proof_url_2     TEXT,
    proof_url_3     TEXT,
    self_views      INTEGER NOT NULL DEFAULT 0,
    self_likes      INTEGER NOT NULL DEFAULT 0,
    self_comments   INTEGER NOT NULL DEFAULT 0,
    verified_views  INTEGER NOT NULL DEFAULT 0,
    verified_likes  INTEGER NOT NULL DEFAULT 0,
    verified_comments INTEGER NOT NULL DEFAULT 0,
    earnings_cents  INTEGER NOT NULL DEFAULT 0,
    fraud_score     INTEGER NOT NULL DEFAULT 0,        -- 0-100
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','paid')),
    review_note     TEXT,
    share_token     TEXT UNIQUE,                       -- short public token for /v/<token>
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at     TEXT
);

CREATE TABLE IF NOT EXISTS view_clicks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    fan_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- NULL = anonymous
    visitor_token   TEXT,                              -- cookie identifier for anonymous dedup
    ip              TEXT,
    ua              TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_view_clicks_sub ON view_clicks(submission_id);
CREATE INDEX IF NOT EXISTS idx_view_clicks_fan ON view_clicks(fan_id);
CREATE INDEX IF NOT EXISTS idx_view_clicks_visitor ON view_clicks(submission_id, visitor_token);
CREATE INDEX IF NOT EXISTS idx_submissions_share_token ON submissions(share_token);

CREATE TABLE IF NOT EXISTS payouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_cents    INTEGER NOT NULL,
    method          TEXT NOT NULL,
    details         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed')),
    reference       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wallet_tx (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,                     -- topup | charge | earning | withdrawal | refund
    amount_cents    INTEGER NOT NULL,                  -- signed
    note            TEXT,
    ref_id          INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    body            TEXT,
    link            TEXT,
    icon            TEXT DEFAULT 'bell',
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trust_marks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    creator_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mark            TEXT NOT NULL CHECK (mark IN ('trusted','blocked')),
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (brand_id, creator_id)
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_brand ON campaigns(brand_id);
CREATE INDEX IF NOT EXISTS idx_submissions_campaign ON submissions(campaign_id);
CREATE INDEX IF NOT EXISTS idx_submissions_creator ON submissions(creator_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_tx(user_id, created_at DESC);
