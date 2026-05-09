import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { migrate } from "./schema.ts";

export const DEFAULT_DB_PATH = join(homedir(), ".ugcspy", "db.sqlite");

export function openDb(path: string = DEFAULT_DB_PATH): Database {
  mkdirSync(dirname(path), { recursive: true });
  const db = new Database(path);
  db.exec("PRAGMA foreign_keys = ON;");
  db.exec("PRAGMA journal_mode = WAL;");
  migrate(db);
  return db;
}
