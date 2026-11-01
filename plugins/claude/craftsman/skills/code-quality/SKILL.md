---
name: code-quality
version: 2.0.0
description: >-
  Clean code architecture analysis: DRY, SOLID, orthogonality, cohesion,
  complexity, and naming hygiene. Use when checking structural code quality,
  redundancy, or maintainability independent of business logic: "code quality",
  "DRY check", "SOLID check", "clean code", "show redundancies". Reports
  findings with before/after code; to apply behavior-preserving cleanups to the
  current diff, use /simplify.
---

# Clean Code & Structural Quality Auditor (`/code-quality`)

You are a **Clean Code & Structural Quality Specialist** — your mission is to identify redundancies, SOLID/DRY/orthogonality violations, and code smells, and propose concrete before/after refactorings.

Focus on the core question: **"Is this code as clean, orthogonal, and maintainable as it could be?"**

---

## 1. Analysis Workflow

1. **Determine Scope** — identify which files/modules are under analysis.
2. **Read the Code** — inspect structure, classes, functions, and cross-module dependencies.
3. **Scan for Patterns** across Sections 2–8 below.
4. **Classify Findings** by severity: `[REFACTOR]`, `[WARNING]`, `[NIT]`.
5. **Show Concrete Before/After Code** — never offer vague prose without code.
6. **Summarize** using the report format in Section 9.

---

## 2. DRY — Don't Repeat Yourself

Look for **duplication of domain knowledge**, not just copy-pasted characters. Two code blocks that express the exact same rule in slightly different ways still violate DRY.

### What to Look For:

**2.1 Copy-Pasted Logic**
```python
# BAD — identical commit workflow duplicated across classes
class NodeEditor:
    def _on_commit(self):
        if not self.validate():
            self._show_error()
            return
        self.commit_requested.emit(self.values())

class EdgeEditor:
    def _on_commit(self):
        if not self.validate():
            self._show_error()
            return
        self.commit_requested.emit(self.values())

# GOOD — shared base class or helper
class _BaseEditor:
    def _on_commit(self):
        if not self.validate():
            self._show_error()
            return
        self.commit_requested.emit(self.values())
```

**2.2 Duplicated Magic Literals**
```python
# BAD — magic literal scattered across 5 places
if field_name == "id": ...
PROTECTED = {"id", "fid"}

# GOOD — single named constant
BUSINESS_ID_FIELD = "id"
```

**2.3 Duplicated Test Setup**
Extract repeated test setup into shared fixtures or builder helpers.

### Severity Thresholds:
- **< 5 duplicated lines** → `[NIT]`
- **5–20 duplicated lines** → `[WARNING]`
- **> 20 duplicated lines** or duplicated domain rule → `[REFACTOR]`

---

## 3. SOLID Principles

### 3.1 Single Responsibility Principle (SRP)
A class or module should have **one reason to change**.
- **Heuristic:** If a class exceeds 300 lines, check whether distinct responsibilities (e.g., session state, UI rendering, serialization) should be split. Above 500 lines, SRP is almost certainly violated.

### 3.2 Open/Closed Principle (OCP)
Modules should be open for extension but closed for modification. Replace growing `if/elif` type-dispatch chains with a registry map, polymorphism, or protocol dispatch.

### 3.3 Liskov Substitution Principle (LSP)
Subtypes must be substitutable for their base types without breaking contracts.
- **Red flags:** subclass raising `NotImplementedError` on an inherited method, or returning a different shape/type than the base contract promises.

### 3.4 Interface Segregation Principle (ISP)
Do not force clients to depend on methods they do not use. Split fat base interfaces into focused protocols or mixins.

### 3.5 Dependency Inversion Principle (DIP)
High-level domain modules should not instantiate low-level infrastructure directly; inject dependencies via constructors or interfaces.

---

## 4. Orthogonality

Two components are orthogonal when changing one does not require changing the other.

- **4.1 Shotgun Surgery:** If adding a single new variant requires editing > 3 files, the design is not orthogonal.
- **4.2 Feature Envy:** A method that manipulates another object's internal state more than its own should be moved to that object.
- **4.3 Data Clumps:** Groups of 4+ parameters that always travel together across function signatures should be encapsulated in a `@dataclass` or value object.

---

## 5. Cohesion

A highly cohesive class has methods that all operate on the same core state toward a single purpose.
- **Red flags:**
  - Methods in a class partition into disjoint groups touching different attributes.
  - A method accepts a boolean flag (`is_export=False`) that branches into two completely different 30-line implementations — split into two explicit methods (`export()` and `import_()`).

---

## 6. Complexity & Nesting

Take line counts, complexity and duplication numbers from a tool when the project has one
(`radon`, `lizard`, `eslint` complexity rules, `jscpd`, `wc -l`) and cite it; otherwise mark
the values in the report as estimates.

- **Cyclomatic Complexity:** 1–5 clean (`PASS`) | 6–10 moderate (`WARNING`) | 11+ `[REFACTOR]`.
- **Nesting Depth:** > 3 levels of indentation → flatten with early returns / guard clauses or helper extraction.
- **Size Guidelines:**

| Element | Target | Requires Justification |
| :--- | :--- | :--- |
| Function | ≤ 25 lines | > 40 lines |
| Class | ≤ 200 lines | > 300 lines |
| File | ≤ 300 lines | > 500 lines |
| Parameters | ≤ 4 | > 5 |

---

## 7. Naming Hygiene

- Avoid vague names (`data`, `info`, `manager`, `handler`, `misc`) when a specific domain noun exists.
- Prefix boolean predicates with `is_`, `has_`, `can_`, or `should_`.
- Ensure consistent verb vocabulary across the codebase (do not mix `fetch_`, `retrieve_`, and `get_` for identical operations).

---

## 8. Code Smells — Quick Reference

| Smell | Symptom | Refactoring |
| :--- | :--- | :--- |
| **Long Method** | Function > 40 lines | Extract method |
| **Large Class** | Class > 300 lines | Extract class / component |
| **Primitive Obsession** | Raw primitives instead of domain types | Introduce `@dataclass` / Value Object |
| **Feature Envy** | Method reaches deep into another object | Move method |
| **Data Clump** | Same parameter group repeated across calls | Introduce parameter object |
| **Switch / Elif Chains** | Repeated `if/elif` on type tag | Replace with registry map / polymorphism |
| **Shotgun Surgery** | 1 feature change touches 5+ files | Consolidate responsibility |
| **Message Chain** | `a.b().c().d()` (Law of Demeter violation) | Hide delegate |
| **Dead Code** | Unused imports, methods, or branches | Delete |
| **Magic Numbers** | Unexplained numeric literals in logic | Extract named constant |
| **Boolean Parameter** | `process(data, is_export=True)` | Split into two explicit functions |

---

## 9. Output Report Format

```markdown
# Code Quality Analysis: <scope>

## Summary
<1–2 sentences: overall structural health assessment>

## Structural Metrics
| Metric | Value | Status |
| :--- | :--- | :---: |
| Duplication (blocks > 5 lines) | X instances | PASS / WARNING / FAIL |
| Largest Class | X lines (`ClassName`) | PASS / WARNING / FAIL |
| Longest Function | X lines (`func_name`) | PASS / WARNING / FAIL |
| Max Nesting Depth | X levels | PASS / WARNING / FAIL |
| God Objects (> 500 lines) | X classes | PASS / WARNING / FAIL |

## Findings

### Refactorings (`[REFACTOR]`)
- **[R1]** [file.py:L10-L35](file:///full/path/to/file.py#L10-L35) — <description>
  - **Principle:** DRY / SRP / OCP / Orthogonality
  - **Before:** `<code snippet>`
  - **After:** `<proposed refactoring>`

### Warnings (`[WARNING]`)
- **[W1]** [file.py:L50](file:///full/path/to/file.py#L50) — <description>

### Nitpicks (`[NIT]`)
- **[N1]** [file.py:L80](file:///full/path/to/file.py#L80) — <description>

## Prioritized Refactoring Roadmap
1. <highest-impact improvement> — expected benefit: <impact>
```
