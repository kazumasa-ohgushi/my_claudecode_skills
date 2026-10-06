# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. It is the index: repo-wide rules live here, and each skill's own conventions live in `<skill>/CLAUDE.md`.

## Repository purpose

A collection of Claude Code skills the user authors and publishes. Each top-level directory is one self-contained skill that users install into `~/.claude/skills/<skill-name>/` (see the skill's README for the curl-based install). This repo is the upstream source — it is not itself loaded as a skill directory.

## Skills

| Skill | What it does | Conventions | Published to |
|---|---|---|---|
| [md-to-gdoc](md-to-gdoc/) | Markdown (+ PNG images) → Google Doc | [md-to-gdoc/CLAUDE.md](md-to-gdoc/CLAUDE.md) | `moloco/moloco-ads-claude-plugins` → `productivity-core/skills/md-to-gdoc/` |

**Before editing a skill, read its `<skill>/CLAUDE.md`** (dev setup, validation fixtures, architecture, pitfalls). When you add a skill, add a row here and create its `CLAUDE.md`.

## Skill layout convention

Each skill directory contains:
- `SKILL.md` — front-matter (`name`, `description`, `argument-hint`, `last_verified`, `owner`) plus the prompt Claude reads when the slash command fires. Includes the "What You Must Do" steps so the model knows how to invoke the script.
- `README.md` — user-facing install + usage docs (referenced by the curl URLs end users run).
- `<skill>.py` — the actual implementation script. Skills assume Python 3.12+ and are invoked with `python3 <abs_path_to_script> ...`.
- `CLAUDE.md` — development conventions for this skill (not installed by users).
- `VALIDATION.md` — validation evidence: what was run, in which modes, and what was checked. Update it after each validation round, and bump `last_verified` in `SKILL.md`.
- `testdata/` (optional) — fixtures used for validation.

When editing a skill, **keep `SKILL.md`, `README.md`, and the script's `--help`/argparse in sync**. The supported-elements table appears in both `.md` files; updating one without the other causes user-visible drift.

## Publishing to moloco-ads-claude-plugins

Skills listed with a plugin destination above are mirrored into `moloco/moloco-ads-claude-plugins` (local clone: `~/git/moloco-ads-claude-plugins`). This repo is the source; the plugin copy follows it.

- Copy only `SKILL.md`, `VALIDATION.md` and the script. `README.md`, `CLAUDE.md` and `testdata/` stay here; in the plugin's `VALIDATION.md`, link to this repo for fixtures.
- The plugin's `SKILL.md` keeps its own front-matter style (unquoted `last_verified`) and its `## On Load` `report_skill_usage` hook.
- Work in a separate worktree branched from `origin/main` — the local clone often has unrelated in-progress changes. Open a PR titled `Update <skill> in <plugin>: ...`.
- Do not bump `plugin.json`; CI does it. Review is by the plugin's code owner (`.github/CODEOWNERS`).
