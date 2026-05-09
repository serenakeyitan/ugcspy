import { Database } from "bun:sqlite";

export function migrate(db: Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS competitors (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      handle TEXT NOT NULL,
      platform TEXT NOT NULL,
      added_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(handle, platform)
    );

    CREATE TABLE IF NOT EXISTS videos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      competitor_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
      platform TEXT NOT NULL,
      external_id TEXT NOT NULL,
      posted_at TEXT NOT NULL,
      fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
      caption TEXT NOT NULL DEFAULT '',
      thumbnail_url TEXT NOT NULL DEFAULT '',
      video_url TEXT NOT NULL DEFAULT '',
      view_count INTEGER NOT NULL DEFAULT 0,
      like_count INTEGER NOT NULL DEFAULT 0,
      comment_count INTEGER NOT NULL DEFAULT 0,
      share_count INTEGER NOT NULL DEFAULT 0,
      hook_source TEXT NOT NULL DEFAULT 'none',
      hook_text TEXT NOT NULL DEFAULT '',
      hook_confidence REAL NOT NULL DEFAULT 0,
      format_tag TEXT,
      raw_metrics_json TEXT NOT NULL DEFAULT '{}',
      UNIQUE(platform, external_id)
    );

    CREATE INDEX IF NOT EXISTS idx_videos_competitor ON videos(competitor_id);
    CREATE INDEX IF NOT EXISTS idx_videos_posted_at ON videos(posted_at);

    CREATE TABLE IF NOT EXISTS watches (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      competitor_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
      slack_webhook_url TEXT NOT NULL,
      threshold_multiplier REAL NOT NULL DEFAULT 2.0,
      state TEXT NOT NULL DEFAULT 'warming_up',
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS alerts_fired (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
      watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
      fired_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(video_id, watch_id)
    );
  `);
}
