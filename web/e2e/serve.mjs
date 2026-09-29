// Start `arc tower serve --v2` for the Playwright run against a scratch DB (never
// data/arc.db). Needs `make web` first (the built SPA) and the repo venv.
//
//   node e2e/serve.mjs            fresh, migrated, empty DB   (shell + kitchen sink)
//   node e2e/serve.mjs --fixture  scripts/tower_fixture_db.py anchored at now (Overview)
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const repo = resolve(import.meta.dirname, "..", "..");
const py = process.env.ARC_PYTHON ?? join(repo, ".venv", "bin", "python");
const fixture = process.argv.includes("--fixture");
const port = fixture
  ? (process.env.ARC_E2E_FIXTURE_PORT ?? "4182")
  : (process.env.ARC_E2E_PORT ?? "4181");
const db = join(mkdtempSync(join(tmpdir(), "arc-tower-e2e-")), "arc.db");

if (fixture) {
  execFileSync(py, [join(repo, "scripts", "tower_fixture_db.py"), db], { stdio: "inherit", cwd: repo });
} else {
  execFileSync(
    py,
    [
      "-c",
      "import sys; from arc.store.db import connect; from arc.store.migrate import migrate; " +
        "c = connect(sys.argv[1]); migrate(c); c.close()",
      db,
    ],
    { stdio: "inherit", cwd: repo },
  );
}

const child = spawn(
  py,
  ["-m", "arc.cli", "tower", "serve", "--v2", "--local", "--port", port, "--db", db],
  { stdio: "inherit", cwd: repo },
);
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => child.kill(sig));
child.on("exit", (code) => process.exit(code ?? 0));
