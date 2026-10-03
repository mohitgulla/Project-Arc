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
To refresh a copy: re-vendor from the pinned upstream, update this table, re-run both install.sh.
