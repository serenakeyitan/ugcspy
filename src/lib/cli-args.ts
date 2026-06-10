import { InvalidArgumentError } from "commander";

// Commander option-arg validators. Bare parseInt/parseFloat let NaN (and
// partial parses) through, which caused real failures downstream:
//   --limit abc      → NaN → rows.slice(0, NaN) → silent zero results
//   --days abc       → NaN → JSON-serialized as null → Python bridge TypeError
//   --interval 6h    → parseInt picks up "6" → 6ms hot loop on the provider
//   --threshold abc  → NaN → SQLite binds NULL → median*null=0 → alert spam
// Kept in lib/ (not cli.ts) so they're unit-testable — importing cli.ts runs
// the program.

export function positiveInt(value: string): number {
  const n = Number.parseInt(value, 10);
  if (!Number.isFinite(n) || String(n) !== value.trim() || n <= 0) {
    throw new InvalidArgumentError("expected a positive integer");
  }
  return n;
}

export function positiveFloat(value: string): number {
  const n = Number.parseFloat(value);
  if (!Number.isFinite(n) || n <= 0 || !/^\d*\.?\d+$/.test(value.trim())) {
    throw new InvalidArgumentError("expected a positive number");
  }
  return n;
}
