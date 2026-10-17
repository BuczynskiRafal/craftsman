---
name: build-freecad
description: >
  Build a distributable FreeCAD plugin package (zip) for the trasownik project.
  Use when the user asks to build, package, or prepare a release of the FreeCAD
  workbench. Trigger phrases: "build freecad", "zbuduj paczkę", "przygotuj build",
  "make dist", "spakuj plugin", "przygotuj release", "zrób zipa", "build paczki".
---

# Build FreeCAD Plugin

## Overview

Build a distributable zip package of the trasownik FreeCAD workbench.
The build process uses the Makefile in `adapters/freecad/`.

## Project Layout

```
trasownik/                    ← monorepo root
├── shared/                   ← source of truth for adapter_spec + icons
├── core/                     ← trasownik-core (JSON-RPC server)
└── adapters/freecad/         ← FreeCAD adapter (workbench)
    ├── Makefile              ← build targets
    ├── InitGui.py            ← FreeCAD entry point
    ├── package.xml           ← FreeCAD metadata
    ├── pyproject.toml        ← version + deps
    ├── freecad/trasownik/    ← Python package
    └── dist/                 ← output (git-ignored)
```

## Build Workflow

Execute the following steps **in order**. The working directory for all
`make` commands is `adapters/freecad/`.

### 1. Clean previous artifacts

```bash
make clean
```

### 2. Vendor shared assets

```bash
make vendor
```

This copies `shared/adapter_spec/` data and `shared/icons/` into the
adapter package. Always run before test or dist — the vendored copies
are the runtime data, and `shared/` is the single source of truth.

### 3. Run linter

```bash
make lint
```

Must pass with zero errors. If lint fails, fix the issues before proceeding.

### 4. Run unit tests

```bash
make test
```

All tests must pass. Do **not** proceed to dist if tests fail.

### 5. Build distributable zip

```bash
make dist
```

Output: `dist/trasownik-freecad-<VERSION>.zip`

Version is extracted automatically from `pyproject.toml`.

### 6. Verify the zip

After building, verify the zip contents:

```bash
zipinfo dist/trasownik-freecad-*.zip
```

**Expected contents** (no dev files):
- `InitGui.py` — FreeCAD workbench entry point
- `package.xml` — FreeCAD addon metadata
- `LICENSE`
- `freecad/trasownik/` — full Python package with:
  - `*.py` source files
  - `adapter_spec/` — contract + presentation JSON + i18n
  - `resources/icons/` — SVG icons
  - `resources/ui/` — Qt UI files
  - `py.typed`

**Must NOT contain**: `tests/`, `.venv/`, `__pycache__/`, `typings/`,
`*.egg-info/`, `pyproject.toml`, `Makefile`, `.ruff_cache/`,
`.pytest_cache/`, `.DS_Store`.

### 7. Report results

Summarize:
- Version built
- Zip file path and size
- Number of files in zip
- Test count and status
- Lint status

## Optional: Integration test

If the user has FreeCAD installed locally, optionally run:

```bash
make test-int
```

This requires `freecadcmd` at `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd`
(or overridden via `FREECADCMD=...`). Do not fail the build if FreeCAD
is not installed — just note that integration tests were skipped.

## Installation instructions (for the user)

After build, remind the user how to install:

1. Unzip `trasownik-freecad-<VERSION>.zip`
2. Copy/move the extracted folder to FreeCAD's `Mod/` directory:
   - macOS: `~/Library/Application Support/FreeCAD/v1-1/Mod/trasownik`
3. Restart FreeCAD
4. The **Trasownik** workbench should appear in the workbench selector

## Error handling

- If `make vendor` fails: check that `shared/` directory exists at repo root
- If `make lint` fails: report errors, do not build
- If `make test` fails: report failures, do not build
- If `make dist` fails: check that `InitGui.py`, `package.xml`, `LICENSE`
  exist in `adapters/freecad/`
