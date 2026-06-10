import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { existsSync, mkdtempSync, rmSync, statSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openDb } from "../src/db/index.ts";
import { migrate } from "../src/db/schema.ts";

// Regression tests for the videos uniqueness model. The original schema used a
// GLOBAL UNIQUE(platform, external_id), which meant a single video could only
// belong to ONE competitor. So when a brand-hashtag search (#befreed) stored a
// creator's videos, a later direct pull of that creator (@jacob.befreed) could
// not re-home them — the upsert UPDATE'd the existing rows in place, and the
// creator's view returned only the videos no prior search had claimed. The fix
// is per-competitor uniqueness: UNIQUE(competitor_id, platform, external_id).

function freshDb(): Database {
  const db = new Database(":memory:");
  migrate(db);
  return db;
}

function addCompetitor(db: Database, handle: string): number {
  const r = db
    .prepare(
      `INSERT INTO competitors (handle, platform) VALUES (?, 'tiktok') RETURNING id`,
    )
    .get(handle) as { id: number };
  return r.id;
}

function addVideo(db: Database, competitorId: number, externalId: string): void {
  db.prepare(
    `INSERT INTO videos (competitor_id, platform, external_id, posted_at)
     VALUES (?, 'tiktok', ?, '2026-01-01T00:00:00+00:00')
     ON CONFLICT(competitor_id, platform, external_id) DO UPDATE SET
       posted_at = excluded.posted_at`,
  ).run(competitorId, externalId);
}

describe("videos uniqueness is per-competitor", () => {
  test("same video can belong to two competitors (brand search + creator pull)", () => {
    const db = freshDb();
    const brand = addCompetitor(db, "#befreed");
    const creator = addCompetitor(db, "@jacob.befreed");

    // The brand search stores a video; the creator's own pull stores the same id.
    addVideo(db, brand, "vid-1");
    addVideo(db, creator, "vid-1");

    const brandCount = (
      db
        .prepare(`SELECT COUNT(*) n FROM videos WHERE competitor_id = ?`)
        .get(brand) as { n: number }
    ).n;
    const creatorCount = (
      db
        .prepare(`SELECT COUNT(*) n FROM videos WHERE competitor_id = ?`)
        .get(creator) as { n: number }
    ).n;

    // Both views keep the video — neither overwrites the other.
    expect(brandCount).toBe(1);
    expect(creatorCount).toBe(1);
    expect(
      (db.prepare(`SELECT COUNT(*) n FROM videos`).get() as { n: number }).n,
    ).toBe(2);
  });

  test("creator pull is complete even when a brand search claimed its videos first", () => {
    const db = freshDb();
    const brand = addCompetitor(db, "#befreed");
    const creator = addCompetitor(db, "@jacob.befreed");

    // Brand search first claims 3 of the creator's videos.
    for (const id of ["a", "b", "c"]) addVideo(db, brand, id);
    // Creator's own catalog has 5 videos (the 3 above + 2 more).
    for (const id of ["a", "b", "c", "d", "e"]) addVideo(db, creator, id);

    const creatorVids = db
      .prepare(`SELECT external_id FROM videos WHERE competitor_id = ? ORDER BY external_id`)
      .all(creator) as { external_id: string }[];

    // The creator view shows the FULL catalog (5), not just the 2 unclaimed.
    expect(creatorVids.map((v) => v.external_id)).toEqual(["a", "b", "c", "d", "e"]);
  });

  test("re-upsert within the same competitor stays idempotent (no duplicates)", () => {
    const db = freshDb();
    const c = addCompetitor(db, "@x");
    addVideo(db, c, "dup");
    addVideo(db, c, "dup");
    addVideo(db, c, "dup");
    expect(
      (db.prepare(`SELECT COUNT(*) n FROM videos WHERE competitor_id = ?`).get(c) as { n: number })
        .n,
    ).toBe(1);
  });
});

describe("migration from the old global-unique schema", () => {
  test("rebuilds an old (platform, external_id)-unique videos table to per-competitor", () => {
    const db = new Database(":memory:");
    // Stand up the OLD schema by hand: competitors + a videos table with the
    // legacy global unique constraint, plus a couple rows.
    db.exec(`
      CREATE TABLE competitors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        handle TEXT NOT NULL, platform TEXT NOT NULL, added_at TEXT DEFAULT (datetime('now')),
        UNIQUE(handle, platform)
      );
      CREATE TABLE videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        competitor_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
        platform TEXT NOT NULL, external_id TEXT NOT NULL, posted_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
        caption TEXT NOT NULL DEFAULT '', thumbnail_url TEXT NOT NULL DEFAULT '',
        video_url TEXT NOT NULL DEFAULT '', view_count INTEGER NOT NULL DEFAULT 0,
        like_count INTEGER NOT NULL DEFAULT 0, comment_count INTEGER NOT NULL DEFAULT 0,
        share_count INTEGER NOT NULL DEFAULT 0, hook_source TEXT NOT NULL DEFAULT 'none',
        hook_text TEXT NOT NULL DEFAULT '', hook_confidence REAL NOT NULL DEFAULT 0,
        format_tag TEXT, raw_metrics_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(platform, external_id)
      );
      INSERT INTO competitors (handle, platform) VALUES ('#befreed','tiktok');
      INSERT INTO videos (competitor_id, platform, external_id, posted_at)
        VALUES (1, 'tiktok', 'keep-me', '2026-01-01T00:00:00+00:00');
    `);

    // Sanity: old constraint present, old data present.
    const before = db
      .prepare(`SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'`)
      .get() as { sql: string };
    expect(/UNIQUE\s*\(\s*platform\s*,\s*external_id\s*\)/i.test(before.sql)).toBe(true);

    // Run migrate() — should detect the old shape and rebuild.
    migrate(db);

    const after = db
      .prepare(`SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'`)
      .get() as { sql: string };
    expect(/competitor_id\s*,\s*platform\s*,\s*external_id/i.test(after.sql)).toBe(true);

    // Existing data survives the rebuild.
    const rows = db.prepare(`SELECT external_id FROM videos`).all() as { external_id: string }[];
    expect(rows.map((r) => r.external_id)).toEqual(["keep-me"]);

    // And the new per-competitor behavior now works.
    const creator = (
      db
        .prepare(`INSERT INTO competitors (handle, platform) VALUES ('@someone','tiktok') RETURNING id`)
        .get() as { id: number }
    ).id;
    db.prepare(
      `INSERT INTO videos (competitor_id, platform, external_id, posted_at)
       VALUES (?, 'tiktok', 'keep-me', '2026-01-01T00:00:00+00:00')`,
    ).run(creator);
    expect((db.prepare(`SELECT COUNT(*) n FROM videos`).get() as { n: number }).n).toBe(2);

    // The rebuild must carry the transcript columns (step 2 ALTERs them in
    // BEFORE the rebuild). Regression: the first transcript command after a
    // legacy upgrade used to die with "no such column: transcript".
    db.prepare(
      `UPDATE videos SET transcript = 'spoken words', transcript_kind = 'speech',
       transcript_words = 2, transcribed_at = datetime('now') WHERE external_id = 'keep-me'`,
    ).run();
    const t = db
      .prepare(`SELECT transcript_kind FROM videos WHERE transcript = 'spoken words' LIMIT 1`)
      .get() as { transcript_kind: string };
    expect(t.transcript_kind).toBe("speech");
  });

  test("migrate() is idempotent on an already-current schema", () => {
    // Also covers the ALTER loop's error filter: the second migrate() hits
    // "duplicate column name" for author_handle, which must stay swallowed.
    const db = freshDb(); // already per-competitor
    expect(() => migrate(db)).not.toThrow();
    const ddl = db
      .prepare(`SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'`)
      .get() as { sql: string };
    expect(/competitor_id\s*,\s*platform\s*,\s*external_id/i.test(ddl.sql)).toBe(true);
  });
});

describe("openDb hardening (temp path — never ~/.ugcspy)", () => {
  test("sets busy_timeout=5000 and locks the db dir/files down to owner-only", () => {
    const dir = mkdtempSync(join(tmpdir(), "ugcspy-db-test-"));
    const dbDir = join(dir, "state");
    const path = join(dbDir, "db.sqlite");
    try {
      const db = openDb(path);
      const bt = db.prepare("PRAGMA busy_timeout").get() as { timeout: number };
      expect(bt.timeout).toBe(5000);
      // The DB stores Slack webhook URLs — same protection as config.json.
      expect(statSync(path).mode & 0o777).toBe(0o600);
      expect(statSync(dbDir).mode & 0o777).toBe(0o700);
      for (const sidecar of [`${path}-wal`, `${path}-shm`]) {
        if (existsSync(sidecar)) {
          expect(statSync(sidecar).mode & 0o777).toBe(0o600);
        }
      }
      db.close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("does NOT chmod a pre-existing custom parent dir (only the managed ~/.ugcspy dir is repaired)", () => {
    const dir = mkdtempSync(join(tmpdir(), "ugcspy-db-test-"));
    try {
      // A user-owned project folder at 0755 — openDb must not tighten it to
      // 0700 and break unrelated sibling files (codex review P2).
      chmodSync(dir, 0o755);
      const db = openDb(join(dir, "db.sqlite"));
      db.close();
      expect(statSync(dir).mode & 0o777).toBe(0o755);
      expect(statSync(join(dir, "db.sqlite")).mode & 0o777).toBe(0o600);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
