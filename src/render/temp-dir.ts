import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Per-process private directory for render outputs (clips, voiceovers).
 *
 * Previously every provider wrote into a FIXED, world-guessable
 * `join(tmpdir(), "ugcspy-renders")` created with default permissions. On a
 * multi-user host tmpdir() is the shared /tmp, so another local user could
 * pre-create (and thereby own) that directory, read every rendered
 * clip/voiceover, and pre-place or swap files at the predictable paths that
 * compose.py later consumes — `mkdir {recursive}` succeeds silently on a
 * foreign-owned dir, and writeFileSync follows symlinks.
 *
 * mkdtemp fixes both holes atomically: the suffix is unpredictable (no
 * pre-creation) and the dir is created mode 0o700 (no foreign reads).
 *
 * Lazily created once per process and shared by all providers so a single
 * render run keeps its artifacts together.
 */
let renderTempDir: string | null = null;

export function getRenderTempDir(): string {
  if (renderTempDir === null) {
    renderTempDir = mkdtempSync(join(tmpdir(), "ugcspy-renders-"));
  }
  return renderTempDir;
}
