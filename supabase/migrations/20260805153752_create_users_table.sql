/*
# Create users table for Telegram bot

1. New Tables
- `users`
- `user_id` (bigint, primary key) — Telegram user ID
- `status` (text, default 'free') — user plan status
- `is_active` (boolean, default true) — whether the user hasn't blocked the bot
- `joined_date` (timestamptz, default now()) — when the user first started the bot
- `daily_download_count` (int4, default 0) — downloads today
- `last_download_date` (timestamptz, nullable) — date of last download (for daily reset)

2. Security
- Enable RLS on `users`.
- The bot runs server-side and connects with the service role / postgres connection,
  so it bypasses RLS. Policies are set to allow anon/authenticated as a safety net
  but the bot's service connection is the primary accessor.
*/

CREATE TABLE IF NOT EXISTS users (
    user_id bigint PRIMARY KEY,
    status text NOT NULL DEFAULT 'free',
    is_active boolean NOT NULL DEFAULT true,
    joined_date timestamptz NOT NULL DEFAULT now(),
    daily_download_count int4 NOT NULL DEFAULT 0,
    last_download_date timestamptz
);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "users_select_all" ON users;
CREATE POLICY "users_select_all" ON users FOR SELECT
TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "users_insert_all" ON users;
CREATE POLICY "users_insert_all" ON users FOR INSERT
TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "users_update_all" ON users;
CREATE POLICY "users_update_all" ON users FOR UPDATE
TO anon, authenticated USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "users_delete_all" ON users;
CREATE POLICY "users_delete_all" ON users FOR DELETE
TO anon, authenticated USING (true);
