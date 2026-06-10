import { Database } from "bun:sqlite";
import { chmodSync, existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { migrate } from "./schema.ts";

export const DEFAULT_DB_PATH = join(homedir(), ".ugcspy", "db.sqlite");

export function openDb(path: string = DEFAULT_DB_PATH): Database {
  const onDisk = path !== ":memory:";
  if (onDisk) mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const db = new Database(path);
  db.exec("PRAGMA foreign_keys = ON;");
  db.exec("PRAGMA journal_mode = WAL;");
  // Concurrent writers (a daemon tick + a manual search) get 5s of retry
  // instead of an instant SQLITE_BUSY error.
  db.exec("PRAGMA busy_timeout = 5000;");
  migrate(db);
  if (onDisk) {
    // The DB stores Slack webhook URLs (write credentials) — keep it owner-only
    // like config.json. Best-effort, same pattern as saveConfig: repairs the
    // 0755 dir / 0644 files of existing installs, covers the WAL sidecars, and
    // skips silently on platforms without POSIX modes (win32). The DIRECTORY
    // chmod is restricted to the managed ~/.ugcspy dir — a custom db path may
    // live inside a directory we don't own (e.g. a project folder), and
    // tightening that to 0700 could break unrelated sibling files.
    try {
      if (dirname(path) === dirname(DEFAULT_DB_PATH)) {
        chmodSync(dirname(path), 0o700);
      }
      for (const p of [path, `${path}-wal`, `${path}-shm`]) {
        if (existsSync(p)) chmodSync(p, 0o600);
      }
    } catch {
      /* noop */
    }
  }
  return db;
}
