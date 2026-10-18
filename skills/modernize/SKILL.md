---
name: modernize
description: >-
  Engineering modernization, architectural refactoring, and legacy code
  migration. Extracts hidden domain and computational rules into Given/When/Then
  Rule Cards first, builds characterization tests, and then executes the
  uplift/transformation with equivalence verification. Use when /modernize is
  invoked.
---

# Code Modernization & Rule Extraction (`/modernize`)

You lead safe modernization, refactoring, and migration of complex or legacy code.

Core Principle: **Never rewrite a legacy module from memory — first separate *what the domain/mathematics requires* from *how the legacy code happened to implement it*.**

Include clickable `file://` Markdown links for all referenced files and symbols.

---

## 5-Stage Modernization Process

### Stage 1: Preflight & Assessment
1. Analyze the target module/system: size, input/output dependencies, existing test coverage, bottlenecks, and technical debt.
2. Confirm with the user (via `ask_question` if not specified in the prompt) the modernization pattern:
   - **Uplift (Version / Standard Upgrade):** same language and architecture, upgrading libraries, APIs, or typing (e.g., Qt5 → Qt6, legacy Python → modern typed Python, Pandas → Polars/NumPy),
   - **Transform (Module Restructuring):** extracting pure domain logic out of monolithic code (e.g., decoupling computational engines from GUI layers),
   - **Reimagine (New Architecture):** designing a new module that satisfies the same extracted domain rules,
   - **Extract Rules Only:** documenting hidden domain rules and algorithms without modifying code.
3. Establish contract invariants: must behavior remain bit-for-bit identical (including legacy quirks), or are known bugs being fixed during the uplift?

---

### Stage 2: Business & Domain Rules Extraction (`business-rules-extractor`)
Inspect the legacy code and extract all embedded **domain and computational rules** (ignoring pure boilerplate, UI wiring, or logging):
- **Calculations & Formulas:** mathematical/scientific/geometric formulas, coefficients, rounding rules, and units,
- **Validations & Thresholds:** required fields, value ranges, cross-field constraints,
- **State Transitions & Eligibility:** what triggers state changes and required operation ordering,
- **Embedded Constants (*Magic Numbers*):** list all hardcoded numbers and parameters with their exact locations (`[file.py:L10-L20](file:///...)`). *(Security note: if you encounter hardcoded passwords or API keys, mask the value as `<credential — masked>`).*

For each significant rule, produce a concise **Rule Card** with a concrete **`Given / When / Then`** specification and a confidence level (`High` / `Medium` / `Low` — ask the user when `Low`).

---

### Stage 3: Characterization Tests (Safety Net Before Editing)
Before modifying legacy production code:
1. Turn the Stage 2 `Given / When / Then` Rule Cards into automated **characterization / golden tests**.
2. Run them against the **legacy code** to confirm they pass and lock in current reference behavior.

---

### Stage 4: Transformation / Uplift (`architecture-critic` & `uplift-migrator`)
1. Implement the target architecture or migration in logical, atomic steps.
2. Ensure every extracted rule from Stage 2 has an explicit, readable home in the modernized code (replacing magic numbers with named constants or configuration parameters).

---

### Stage 5: Equivalence Verification (`modernize-verify`)
1. Run the Stage 3 characterization test suite against the modernized code.
2. Compare outputs (output isomorphism, performance, error handling).
3. Present a final summary table listing preserved domain rules, architectural improvements, and any intentionally updated behaviors.
