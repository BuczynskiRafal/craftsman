# 🛠️ Craftsman — Master Software Engineering Suite for Antigravity

Kompletny zestaw inżynierskich skilli, audytorów, orkiestratora `/craftsman` oraz globalnych reguł (`ultrathink` / `ultracode` + higiena Git) dla środowiska **Google Antigravity**. Zaprojektowany jako inżynieryjny odpowiednik naukowego orkiestratora `/k-dance`, łączący najlepsze praktyki z wbudowanych narzędzi i oficjalnych pluginów Claude Code v2.1.287 oraz autorskich skilli optymalizacyjnych.

---

## 🚀 Szybka instalacja na nowej maszynie

```bash
git clone https://github.com/BuczynskiRafal/craftsman.git ~/.gemini/craftsman
cd ~/.gemini/craftsman
./install.sh --symlink
```

### Tryby instalacji (`./install.sh`):
- `./install.sh --copy` *(domyślny)* — kopiuje skille do `~/.gemini/config/skills/` oraz reguły do `~/.gemini/config/rules/`.
- `./install.sh --symlink` *(zalecany)* — tworzy dowiązania symboliczne do sklonowanego repozytorium, dzięki czemu zwykłe `git pull` od razu aktualizuje wszystkie skille i reguły.
- `./install.sh --plugin` — instaluje cały pakiet jako przełączalny plugin Antigravity w `~/.gemini/config/plugins/craftsman` (zarządzany przez `/plugin`).

---

## 🧭 Architektura Orkiestratora `/craftsman`

```mermaid
flowchart TD
    User(["Użytkownik: /craftsman [zadanie]"]) --> Triage["Stage 0: Triage i Klasyfikacja (Typ A-E)"]

    Triage -->|"Typ A: Greenfield / Feature"| Arch["Stage 1A: /feature-dev (Eksploracja -> Pytania -> Architektura)"]
    Triage -->|"Typ B: Legacy / Refactor"| Mod["Stage 1B: /modernize (Karta Reguł Given/When/Then -> Testy)"]
    Triage -->|"Typ C: Masowa migracja"| Batch["Stage 1C: /batch (Podział na 5-30 Work Units -> Subagenty)"]
    Triage -->|"Typ D: Performance / Hot-path"| Perf["Stage 1D: /extreme_optimizer (Benchmark -> Profilowanie -> Izomorfizm)"]
    Triage -->|"Typ E: Bugfix / Hardening"| Bug["Stage 1E: Root-Cause Analysis (Reprodukcja -> Test regresyjny)"]

    Arch --> Impl["Stage 2: Implementacja"]
    Mod --> Impl
    Batch --> Impl
    Perf --> Impl
    Bug --> Impl

    Impl --> Simplify["Stage 3.1: /simplify (Reuse, Simplification, Efficiency, Altitude)"]
    Simplify --> Review["Stage 3.2: /code-review + /audit (Kąty A-E + audyty specjalistyczne)"]
    Review --> Verify["Stage 3.3: /verify (Realne uruchomienie w runtime: CLI / API / GUI)"]
    Verify -->|"Wykryto błędy (max 2 iteracje)"| Impl
    Verify -->|"PASS"| Commit["Stage 4: /high-level-commit-message (Raport + propozycja commita)"]
```

---

## 📦 Zawartość pakietu (20 Skilli + 2 Reguły Globalne)

### 1. Główny Orkiestrator
| Komenda | Opis |
| :--- | :--- |
| **`/craftsman`** | Główny dyrygent inżynierski end-to-end. Samodzielnie rozpoznaje typ zadania (A–E), dobiera workflow architektoniczny, przeprowadza implementację i zamyka ją automatyczną bramką jakości z pętlą samonaprawy (`--fast`, `--deep`, `--plan-only`). |

### 2. Workflowy Architektoniczne i Transformacyjne
| Komenda | Opis |
| :--- | :--- |
| **`/feature-dev`** | 7-fazowa budowa nowej funkcjonalności: eksploracja kodu -> pytania doprecyzowujące -> 2–3 warianty architektury -> implementacja -> review. |
| **`/modernize`** | Bezpieczna modernizacja zastanego kodu: ekstrakcja reguł biznesowych do tablicy `Given / When / Then`, testy zabezpieczające (*characterization tests*), refaktor i dowód równoważności. |
| **`/batch`** | Masowe, równoległe migracje i refaktoryzacje (5–30 niezależnych jednostek pracy wykonywanych przez subagenty w tle). |

### 3. Codzienna Bramka Jakości (Core Quality Gate)
| Komenda | Opis |
| :--- | :--- |
| **`/simplify`** | Oczyszczanie świeżo napisanego kodu bez zmiany zachowania (Reuse, Simplification, Efficiency, Altitude + zasady `code-simplifier`). |
| **`/code-review`** | 10-kątowy przegląd kodu (lokalny diff, gałąź lub PR z GitHuba/GitLaba) z adaptacyjnym trybem inline/subagent oraz weryfikacją znalezisk (`CONFIRMED` / `PLAUSIBLE` / `REFUTED`). |
| **`/verify`** | Weryfikacja zmian end-to-end w prawdziwym środowisku uruchomieniowym (CLI, serwer HTTP, pipeline danych, headless FreeCAD/QGIS) zamiast polegania wyłącznie na suchych testach jednostkowych. |
| **`/high-level-commit-message`** | Generowanie zwięzłych, wysokopoziomowych komunikatów commitów w standardzie Conventional Commits (American English). |

### 4. Specjalistyczne Audyty Głębokie (`/audit`)
| Komenda | Opis |
| :--- | :--- |
| **`/audit`** | Orkiestrator audytów uruchamiający wybrane lub wszystkie poniższe audyty równolegle (`/audit all`, `/audit errors,security`). |
| **`/audit-errors`** | *Silent Failure Hunter* — polowanie na połknięte wyjątki, zbyt szerokie `try/except` i ciche fallbacki zwracające `None`/`0`/`[]`. |
| **`/audit-tests`** | *Behavioral Test Coverage* — ocena pokrycia behawioralnego, warunków brzegowych i odporności testów na refaktoryzację (skala 1–10). |
| **`/audit-types`** | *Type & Invariant Analyzer* — ocena modeli danych, dataclass i Pydantic na 4 osiach (*make illegal states unrepresentable*). |
| **`/audit-comments`** | *Comment Rot Analyzer* — krzyżowa weryfikacja docstringów i komentarzy względem faktycznej implementacji. |
| **`/audit-security`** | *Source-to-Sink Taint Auditor* — analiza bezpieczeństwa przepływu danych z oceną CWE i minimalnymi łatkami. |

### 5. Wydajność, Czystość Kodu i Narzędzia Domenowe
| Komenda | Opis |
| :--- | :--- |
| **`/code-quality`** | Analiza czystości architektury pod kątem DRY, SOLID, ortogonalności i spójności. |
| **`/extreme-optimizer`** | Iteracyjna, oparta na profilowaniu optymalizacja wydajności z wymogiem benchmarku i dowodu izomorfizmu wyników. |
| **`/leetcode-style-code-reviewer`** | Bezlitosny przegląd wydajnościowy kodu Python/NumPy/PyTorch w stylu finalisty ICPC. |
| **`/humanizer`** | Usuwanie śladów sztucznego stylu AI z dokumentacji, tekstów i opisów. |
| **`/build-freecad`** | Budowanie paczki dystrybucyjnej `.zip` dla workbenchu FreeCAD (`trasownik`). |
| **`/deploy-freecad-local`** | Lokalny deployment paczki `.zip` do katalogu `Mod` FreeCADa w celu testów manualnych. |

---

## ⚡ Globalne Reguły (`rules/`) i Hybrydowy Hard-Loop (`hooks.json` + `scripts/`)

### Reguły kontekstowe (`rules/`)
1. **`ultra_mode.md`** — Słowa-klucze wyzwalające rygor inżynierski w dowolnym prompcie:
   - **`ultrathink`** — wymusza sekcję *Deep Reasoning & Architecture Analysis* (założenia, warunki brzegowe, złożoność Czas/Pamięć, porównanie min. 2 podejść) przed napisaniem pierwszej linijki kodu.
   - **`ultracode`** — wymusza pełny rygor wykonawczy w trybie Hard-Loop: `Explore -> Plan (ultrathink) -> Implement -> /simplify -> /code-review -> /verify`.
2. **`git_workflow.md`** — Ochrona gałęzi `main`/`master` przed niepotwierdzonym `git push`, ścisła tożsamość autora (`Rafał Buczyński`), zero atrybucji AI w commitach oraz atomowe commity Conventional Commits.

### Deterministyczne Strażniki Systemowe (`hooks.json` + `scripts/`)
Pakiet wykorzystuje natywne **Lifecycle Hooks** Antigravity do fizycznego egzekwowania kluczowych niezmienników poza modelem:
1. **`craftsman-git-guard` (`PreToolUse` -> `scripts/git-guard.py`):**
   - **Zero AI Attribution (Global Rule #2):** Przechwytuje wywołania `git commit`, `git tag`, `git merge` oraz `gh pr create` zawierające `Co-authored-by:` lub stopki `Generated by/with ...` i blokuje je na poziomie procesu (`"decision": "deny"`).
   - **Ochrona `main`/`master` (Global Rule #1):** Przechwytuje `git push` celujący w `main` lub `master`, sprawdza w `transcript.jsonl`, czy po ostatnim pushu wywołano `ask_question` z pytaniem do użytkownika, i blokuje nieautoryzowane wypchnięcie (`"decision": "deny"`, lub `"force_ask"` przy `--force`).
2. **`craftsman-stop-gate` (`Stop` -> `scripts/craftsman-stop-gate.py`):**
   - Podczas aktywnej sesji `/craftsman` lub `ultracode` sprawdza plik stanu `<artifactDirectoryPath>/craftsman_state.json`.
   - Dopóki bramki `simplified`, `reviewed` oraz `verified` nie mają wartości `true` (i agent nie czeka na decyzję użytkownika w `ask_question`), blokuje przedwczesne zakończenie tury (`{"decision": "continue"}`), wymuszając domknięcie pętli jakości (z bezpiecznikiem maksymalnie 3 wymuszonych kontynuacji).
   - Przy zwykłych rozmowach (gdy `craftsman_state.json` nie istnieje lub ma `"active": false`) działa w trybie przezroczystym bez narzutu.
