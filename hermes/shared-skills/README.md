# Shared helper skills for the reviewer profiles

Tool how-tos installed next to the reviewer's own skill by `hermes/sentinel/install.sh` and
`hermes/analyst/install.sh` (PLAN D42). They are vendored copies, pinned here so the reviewers
stay independent of `~/.hermes/skills/` (the implementation profile). The reviewer's own SKILL.md
always wins: its fetch caps, primary-source rule and read-only limits apply to every helper.

| Skill | Upstream (pinned) | Installed into |
|---|---|---|
| `defuddle` | kepano/obsidian-skills@3ccff53 skills/defuddle (MIT) | sentinel, analyst |
| `agent-reach` | Panniantong/agent-reach@a19a171 agent_reach/skill/SKILL_en.md (MIT); description narrowed | sentinel, analyst |
| `code-review-and-quality` | addyosmani/agent-skills@a06bc63 (MIT); ../../references links vendored into references/ | sentinel |
| `security-and-hardening` | addyosmani/agent-skills@a06bc63 (MIT); ../../references links vendored into references/ | sentinel |
| `performance-optimization` | addyosmani/agent-skills@a06bc63 (MIT); ../../references links vendored into references/ | sentinel |

Host tools they call (installed once per machine, outside the repo): `defuddle` (npm -g),
`agent-reach` + `yt-dlp` (uv tool), `mcporter` with the `exa` server (npm -g, `~/.mcporter`).
Updates: the weekly Sentinel run checks every pin against upstream
(`~/.hermes/scripts/skills_upstream_sync.py --check`, manifest
`~/.hermes/scripts/state/skills_upstream.json`) and files a `deps:skills-upstream` card when one
moved. That card's worker runs `--apply --repo-dir <worktree>`, which rebuilds the copies, rescans them
with Hermes skills_guard and rewrites the pins in this table. Its PR goes through normal review. A
scan finding that hasn't been reviewed yet is held for the owner. After merge, re-run both install.sh.
