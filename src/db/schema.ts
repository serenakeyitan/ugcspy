import { Database } from "bun:sqlite";

export function migrate(db: Database): void {
  // 1. Create tables (no-op if they exist)
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
      author_handle TEXT,
      raw_metrics_json TEXT NOT NULL DEFAULT '{}',
      -- Scope uniqueness to the competitor. A video can legitimately appear in
      -- more than one view — e.g. the same clip shows up under a brand hashtag
      -- search (#yourbrand) AND under the creator's own catalog (@creator.handle).
      -- A global UNIQUE(platform, external_id) made the second writer UPDATE the
      -- first's row instead of inserting, so whichever view ran second looked
      -- truncated (a creator's full pull returned only the videos no prior brand
      -- search had already claimed). Per-competitor keying fixes that.
      UNIQUE(competitor_id, platform, external_id)
    );

    CREATE INDEX IF NOT EXISTS idx_videos_competitor ON videos(competitor_id);
    CREATE INDEX IF NOT EXISTS idx_videos_posted_at ON videos(posted_at);
    -- saveTranscript() looks up by (platform, external_id) on every transcribed
    -- video — the index changes the access path only, never the result set.
    -- (platform/external_id always exist; safe in this fresh block. The
    -- author_handle index is created AFTER the ALTER loop below — on a legacy DB
    -- author_handle doesn't exist yet here.)
    CREATE INDEX IF NOT EXISTS idx_videos_platform_external_id ON videos(platform, external_id);

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

  // 2. Forward migrations: add new columns to existing rows. SQLite has no
  // IF NOT EXISTS for columns; attempt the ALTER and swallow "duplicate
  // column" errors. Order matters — older DBs without these columns get
  // patched to current shape on every open.
  for (const stmt of [
    `ALTER TABLE videos ADD COLUMN author_handle TEXT`,
    // Whisper transcript cache (ugcspy transcript) — one-shot per video; the
    // audio of a posted clip never changes, so no re-transcribe path needed.
    `ALTER TABLE videos ADD COLUMN transcript TEXT`,
    `ALTER TABLE videos ADD COLUMN transcript_kind TEXT`,
    `ALTER TABLE videos ADD COLUMN transcript_lang TEXT`,
    `ALTER TABLE videos ADD COLUMN transcript_words INTEGER`,
    `ALTER TABLE videos ADD COLUMN transcript_duration_sec REAL`,
    `ALTER TABLE videos ADD COLUMN transcribed_at TEXT`,
  ]) {
    try {
      db.exec(stmt);
    } catch (err) {
      // Only "duplicate column name" means already-migrated. Anything else
      // (disk I/O, malformed DB, locked file) must surface, not be swallowed
      // into a silently half-migrated schema.
      if (!/duplicate column name/i.test((err as Error).message)) throw err;
    }
  }

  // author_handle index — created HERE (after the ALTER loop guarantees the
  // column exists), not in the step-1 CREATE block: on a legacy DB the column
  // is ADDed above, so a step-1 index would hit "no such column". Speeds
  // cachedMaxViews() (ugcspy similar); access-path only, never the result set.
  // The rebuild block below re-creates it (DROP TABLE there discards it).
  db.exec(`CREATE INDEX IF NOT EXISTS idx_videos_author_handle ON videos(author_handle);`);

  // 3. Migrate the videos unique constraint from the old global
  // UNIQUE(platform, external_id) to per-competitor UNIQUE(competitor_id,
  // platform, external_id). The old key let one competitor's upsert silently
  // overwrite another's row for the same video, so a creator's full catalog
  // pull returned only the videos no prior brand search had already claimed.
  // SQLite can't ALTER a constraint, so rebuild the table when the old shape is
  // detected. Detection: the old index/constraint shows up in the table's SQL.
  const ddl = db
    .prepare(`SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'`)
    .get() as { sql?: string } | undefined;
  const needsRebuild =
    !!ddl?.sql &&
    /UNIQUE\s*\(\s*platform\s*,\s*external_id\s*\)/i.test(ddl.sql) &&
    !/competitor_id\s*,\s*platform\s*,\s*external_id/i.test(ddl.sql);
  if (needsRebuild) {
    db.exec("PRAGMA foreign_keys=OFF;");
    const tx = db.transaction(() => {
      db.exec(`
        CREATE TABLE videos_new (
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
          author_handle TEXT,
          raw_metrics_json TEXT NOT NULL DEFAULT '{}',
          transcript TEXT,
          transcript_kind TEXT,
          transcript_lang TEXT,
          transcript_words INTEGER,
          transcript_duration_sec REAL,
          transcribed_at TEXT,
          UNIQUE(competitor_id, platform, external_id)
        );
        INSERT OR IGNORE INTO videos_new
          SELECT id, competitor_id, platform, external_id, posted_at, fetched_at,
                 caption, thumbnail_url, video_url, view_count, like_count,
                 comment_count, share_count, hook_source, hook_text, hook_confidence,
                 format_tag, author_handle, raw_metrics_json,
                 transcript, transcript_kind, transcript_lang,
                 transcript_words, transcript_duration_sec, transcribed_at
          FROM videos;
        DROP TABLE videos;
        ALTER TABLE videos_new RENAME TO videos;
        CREATE INDEX IF NOT EXISTS idx_videos_competitor ON videos(competitor_id);
        CREATE INDEX IF NOT EXISTS idx_videos_posted_at ON videos(posted_at);
        -- mirror of the fresh-block hot-path indexes (see above) — the DROP
        -- TABLE above discards the originals, so a legacy upgrade must recreate them.
        CREATE INDEX IF NOT EXISTS idx_videos_platform_external_id ON videos(platform, external_id);
        CREATE INDEX IF NOT EXISTS idx_videos_author_handle ON videos(author_handle);
      `);
    });
    tx();
    db.exec("PRAGMA foreign_keys=ON;");
  }
}
