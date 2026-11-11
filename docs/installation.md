# Installation Guide

Craftsman requires Python 3.10 or newer and relies strictly on the standard library (zero third-party dependencies).

---

## Quickstart

Install Craftsman using `pipx` or `uv tool`:

```bash
pipx install craftsman-gate
# or:
uv tool install craftsman-gate
```

Upgrading from a `pip install .` of a checkout older than 4.0, which installed the distribution under the
name `craftsman`? Run `pip uninstall craftsman` first: both names install the same files, and uninstalling
the old one later would delete them from under `craftsman-gate`. `craftsman doctor` reports the leftover.

Then run the installer to automatically configure your active environments:

```bash
craftsman install                  # auto-detects Copilot, Claude Code, Antigravity
```

---

## Automated Installation

For manual or scoped installation, pass target flags to `craftsman install` (or `./install.sh`):

```bash
craftsman install --copilot                    # VS Code + Copilot CLI: instructions, skills, agents, hooks
craftsman install --claude                     # Claude Code: commands, agents, gate hooks
craftsman install --antigravity                # Google Antigravity
craftsman install --copilot-workspace ~/src/app   # Commit .github/{skills,agents,hooks} to a repo
craftsman install --agents-workspace ~/src/app    # AGENTS.md + .cursor/rules for Codex and Cursor
```

### Installation Flags

| `craftsman install` / `install.sh` flag | Effect |
| :--- | :--- |
| *(none)* / `--auto` | Detects installed platforms and installs for each. |
| `--copilot` | Global instructions (`craftsman-rules.instructions.md` per VS Code profile), Agent Skills (`~/.copilot/skills/<skill>/SKILL.md`), three custom agents (`~/.copilot/agents/<name>.agent.md`), and gate hooks (`~/.copilot/hooks/craftsman.json`). |
| `--claude` | Commands, rules, reviewer and verifier agents in `~/.claude`, gate hooks in `settings.json`. `--no-claude-hooks` leaves hooks out. |
| `--antigravity` | Skills, rules, scripts, and `hooks.json` in `~/.gemini/config`. |
| `--copy` / `--symlink` / `--plugin` | How Antigravity files are installed: copies (default), symlinks, or an Antigravity plugin in `~/.gemini/config/plugins/craftsman`. |
| `--copilot-workspace <dir>` | `.github/skills`, `.github/agents`, `.github/hooks/craftsman.json`, and a marked block in `.github/copilot-instructions.md`. Workspace hooks call `craftsman` from `PATH`. |
| `--allow-duplicate-scopes` | Overrides the default refusal when installing `--copilot-workspace` on a machine with a user-level `--copilot` install (or `--claude` with an active Claude plugin, or `--antigravity` with an enabled agy plugin). |
| `--agents-workspace <dir>` | Marked block in `AGENTS.md` and `.cursor/rules/craftsman.mdc`. |
| `--all` | All platforms, without auto-detection. |
| `--dry-run` | Prints the install or uninstall plan without modifying files. |

---

## GitHub Copilot Discovery Locations

VS Code Copilot and GitHub Copilot CLI scan multiple directories simultaneously and merge discovered items without deduplication. To ensure every Craftsman agent and `/skill` slash command appears **exactly once**, Craftsman installs into a single canonical location per scope:

| Entity | User scope (`--copilot`, canonical) | Workspace scope (`--copilot-workspace`, canonical) | Other locations VS Code scans (`doctor` reports duplicates there) |
| :--- | :--- | :--- | :--- |
| **Custom agents** (`craftsman`, `craftsman-reviewer`, `craftsman-verifier`) | `~/.copilot/agents/*.agent.md` | `<workspace>/.github/agents/*.agent.md` | `Code/User/prompts/*.agent.md`, `Code/User/profiles/*/prompts/*.agent.md`, `~/.claude/agents/*.md`, `.claude/agents/*.md` |
| **Slash commands / Agent Skills** (`/craftsman`, `/craft-review`, …) | `~/.copilot/skills/<name>/SKILL.md` | `<workspace>/.github/skills/<name>/SKILL.md` | `Code/User/prompts/*.prompt.md`, `Code/User/profiles/*/prompts/*.prompt.md`, `.github/prompts/*.prompt.md`, `~/.claude/skills/*/SKILL.md` |
| **Global instructions** | `Code/User/prompts/craftsman-rules.instructions.md` (+ each profile) | `<workspace>/.github/copilot-instructions.md` | — |

- Installing both `--copilot` (user) and `--copilot-workspace` (workspace) in the same project displays duplicate agents and commands in VS Code; `craftsman install` refuses by default unless `--allow-duplicate-scopes` is specified.
- If both GitHub Copilot and Claude Code are present on the same system, VS Code scans `~/.claude/agents` by default. `craftsman doctor` detects this collision and suggests adding `"chat.agentFilesLocations": {"~/.claude/agents": false}` to VS Code `settings.json`.

---

## Claude Code Plugin

Claude Code supports installing Craftsman as a standalone plugin:

```bash
/plugin marketplace add BuczynskiRafal/craftsman
/plugin install craftsman@craftsman
```

The repository itself is the plugin: Claude Code loads the skills and the Python package straight from it, and the hooks start the CLI through `bin/launcher.sh`, which needs Python 3.10+ on `PATH`. To prevent duplicate hook executions (hooks firing twice), `craftsman install` skips an auto-detected Claude Code and refuses `--claude` while the Claude Code plugin is active (`--no-claude-hooks` installs the rest without the hooks); use `craftsman uninstall --claude` to clean up user-level hooks installed earlier.

## Codex Plugin

Codex reads the same repository as a plugin marketplace (`.agents/plugins/marketplace.json`, manifest in
`.codex-plugin/plugin.json`):

```bash
codex plugin marketplace add BuczynskiRafal/craftsman
codex plugin add craftsman@craftsman
```

Start a new session; the skills are invoked as `$craftsman:craftsman`, `$craftsman:craft-review` and so on.
To update, run `codex plugin marketplace upgrade craftsman`. The plugin brings the skills only: they call
the `craftsman` CLI, so install it too (`pipx install craftsman-gate`). It declares no hooks, because
Craftsman has no adapter for Codex hook events, so the review is advisory (`SELF-REVIEWED`), as with
`--agents-workspace`. Its own manifest also keeps Codex from falling back to `.claude-plugin/plugin.json`
and loading the Claude Code hooks.

## Cursor Plugin

`.cursor-plugin/` holds the Cursor manifest and marketplace catalog. The plugin adds the skills and an
always-applied rule (`.cursor-plugin/rules/craftsman.mdc`, the same content `--agents-workspace` writes
to `.cursor/rules/craftsman.mdc`). Install it as a local plugin:

```bash
rm -rf ~/.cursor/plugins/local/craftsman
git clone --depth 1 https://github.com/BuczynskiRafal/craftsman ~/.cursor/plugins/local/craftsman
```

Then run **Developer: Reload Window**. On Cursor Teams and Enterprise, local plugins need the admin
setting **Allow Local Plugin Imports**, or an admin imports `BuczynskiRafal/craftsman` as a team
marketplace. As in Codex, the plugin has no hooks, the review is advisory, and the skills need the
`craftsman` CLI on `PATH`.

The plugin's rule replaces `.cursor/rules/craftsman.mdc`: both are always applied, so a workspace that
keeps the `--agents-workspace` copy as well gets the same instructions twice in every turn. Delete that
file (keep `AGENTS.md` for Codex), or skip the plugin in that workspace.

## Antigravity Plugin

Antigravity installs the repository as a plugin straight from GitHub:

```bash
agy plugin install https://github.com/BuczynskiRafal/craftsman
```

agy copies it to `~/.gemini/config/plugins/craftsman` with the skills, the rule and the gate hooks, which run the `scripts/` of the plugin. A copy or symlink install (`craftsman install --antigravity`) would add the same skills and hooks to `~/.gemini/config` a second time, so `craftsman install` skips an auto-detected Antigravity and refuses `--antigravity` while the agy plugin is enabled, and `craftsman doctor` reports both installs side by side. `craftsman install --antigravity --plugin` replaces the agy plugin with a copy of Craftsman's own, which `craftsman uninstall --antigravity` removes again.

---

## Doctor & Diagnostics

Run `craftsman doctor` to audit your installation, detect duplicate hooks, inspect active platforms, and verify runtime dependencies:

```bash
craftsman doctor
craftsman doctor --json            # machine-readable output
```

Doctor checks:
- Python runtime version (>= 3.10)
- Skill, rule, and script completeness
- Git identity and hook paths
- Platform hooks configuration (Copilot, Claude Code, Antigravity)
- Hook collisions between user-level settings and Claude Code plugins

When `craftsman review prepare` prints `Advisory:` instead of `Enforced:`, no Craftsman hook saw the session: run `craftsman doctor` to find the missing or duplicate hooks. The hooks also write what they could not do to `<git-dir>/craftsman/hook-errors.log`, and `craftsman review check` reports it.


---

## Uninstallation & Backups

Each install target writes a `<base>/.craftsman-install.json` manifest recording the paths it created and any user files it merged into. When a preexisting user file is in the way, Craftsman preserves it in a `.craftsman-backup` folder. Merged files (`settings.json`, `hooks.json`, `AGENTS.md`) preserve a one-time `.bak` copy.

To uninstall:

```bash
craftsman uninstall                                # uninstalls all detected user-level targets
craftsman uninstall --copilot                      # uninstall Copilot user files
craftsman uninstall --claude                       # remove Claude Code hooks and agents
craftsman uninstall --antigravity                  # clean up Antigravity skills and rules
craftsman uninstall --copilot-workspace <dir>      # remove workspace hooks and skills
craftsman uninstall --dry-run                      # preview changes without removing files
```

Uninstall cleans only paths registered in `.craftsman-install.json` and restores original configuration files.

Running `craftsman install` again also removes what an earlier install created and this version no longer ships (for example the `code-quality`, `extreme-optimizer`, `high-level-commit-message` and `leetcode-style-code-reviewer` skills, which 5.0 still shipped). Only paths the manifest records are removed; a skill or command of the same name that you wrote yourself stays. A folder Craftsman created is Craftsman's with everything in it: every install replaces it and an upgrade that drops it deletes it, so keep your own skills beside it, not inside it.

A manifest written by Craftsman before 4.0 (it records global git hooks) is no longer migrated: `install`, `uninstall` and `doctor` stop with a message that asks you to delete that file and run `craftsman install` again. If `git config --global core.hooksPath` still points to a Craftsman `git-hooks` folder, unset it with `git config --global --unset core.hooksPath`; `craftsman doctor` reports that setting when it points to `~/.craftsman/git-hooks` or `~/.gemini/config/git-hooks`.
