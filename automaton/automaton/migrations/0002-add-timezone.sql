-- Per-cron timezone. NULL = interpret the cron expression in UTC,
-- which preserves behavior for existing rows.
ALTER TABLE cron_trigger ADD COLUMN timezone TEXT;
