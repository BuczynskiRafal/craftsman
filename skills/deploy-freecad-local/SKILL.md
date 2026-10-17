---
name: deploy-freecad-local
description: >
  Deploy the built trasownik FreeCAD plugin zip into the local FreeCAD Mod
  directory for manual testing. Simulates a real user installation from the
  distributable zip — not a dev symlink. Use when you want to verify that
  the packaged plugin installs and loads correctly in FreeCAD.
  Trigger phrases: "zainstaluj w freecad", "deploy do freecad",
  "wrzuć do Mod", "przetestuj instalację", "deploy local",
  "test install", "zainstaluj paczkę".
---

# Deploy FreeCAD Plugin Locally

## Overview

Deploy the distributable zip produced by `make dist` into FreeCAD's local
`Mod/` directory, simulating a real end-user installation. This verifies
that the packaged plugin loads correctly — as opposed to the dev symlink
(`make install-dev`), which bypasses packaging entirely.

## Prerequisites

This skill assumes the trasownik monorepo is at:
```
/Users/rafalbuczynski/Git/trasownik
```

The FreeCAD adapter lives at:
```
/Users/rafalbuczynski/Git/trasownik/adapters/freecad
```

## FreeCAD Mod directory

macOS default for FreeCAD 1.1:
```
~/Library/Application Support/FreeCAD/v1-1/Mod
```

## Workflow

All commands run from `adapters/freecad/`.

### 1. Remove any existing dev symlink

A dev symlink and a deployed copy cannot coexist — FreeCAD would see
duplicates or the symlink would shadow the real install. Always remove
the symlink first:

```bash
make uninstall-dev
```

### 2. Build the distributable zip

```bash
make clean
make dist
```

This produces `dist/trasownik-freecad-<VERSION>.zip`.

### 3. Remove previous deployment from Mod

```bash
rm -rf ~/Library/Application\ Support/FreeCAD/v1-1/Mod/trasownik
```

### 4. Unzip into Mod directory

```bash
unzip -q dist/trasownik-freecad-*.zip -d /tmp/trasownik-deploy
mv /tmp/trasownik-deploy/trasownik-freecad-*/ ~/Library/Application\ Support/FreeCAD/v1-1/Mod/trasownik
rm -rf /tmp/trasownik-deploy
```

The folder in `Mod/` **must** be named `trasownik` (not
`trasownik-freecad-0.0.1`), because `InitGui.py` and `package.xml`
are at its root and FreeCAD uses the folder name as the addon identifier.

### 5. Verify deployment structure

```bash
ls ~/Library/Application\ Support/FreeCAD/v1-1/Mod/trasownik/
```

Expected output should include at minimum:
- `InitGui.py`
- `package.xml`
- `LICENSE`
- `freecad/` (directory with the Python package)

### 6. Report to user

Summarize:
- Version deployed
- Full path of the deployed workbench
- Remind the user to **restart FreeCAD** to load the new version
- Mention that the workbench should appear in the workbench selector
  as **Trasownik**

## Rollback: restore dev symlink

After testing, the user will likely want to go back to dev mode:

```bash
rm -rf ~/Library/Application\ Support/FreeCAD/v1-1/Mod/trasownik
make install-dev
```

Offer this as a follow-up action.

## Error handling

- If `dist/` is empty or missing: run `make dist` first
- If `Mod/` directory does not exist: create it with `mkdir -p`
- If both symlink and folder exist: remove both, then deploy fresh
