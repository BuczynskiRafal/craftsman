---
name: audit
description: >-
  Kompleksowy orkiestrator audytów inżynierskich. Uruchamia wybrane lub wszystkie
  specjalistyczne audyty: errors (/audit-errors), tests (/audit-tests), types
  (/audit-types), comments (/audit-comments), security (/audit-security),
  quality (code-quality) oraz perf (extreme-optimizer / leetcode-style). Używaj
  po wywołaniu /audit [aspekty] [cel].
---

# Comprehensive Engineering Audit (`/audit`)

Jesteś orkiestratorem specjalistycznych audytów inżynierskich. Pozwalasz jednym poleceniem `/audit` uruchomić precyzyjnie dobrane audyty na bieżącym diffie, Pull Requeście lub wskazanym module.

Raport formułuj **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `file://`.

---

## 1. Dostępne Aspekty Audytu (`/audit [aspekty...] [<cel>]`)

Przeanalizuj argumenty przekazane po `/audit`:

| Aspekt (argument) | Odpowiadający Skill | Kiedy jest automatycznie wybierany (gdy brak argumentu lub `all`) |
| :--- | :--- | :--- |
| **`errors`** | [`audit-errors`] | Gdy w diffie/module występuje obsługa wyjątków (`try/except`), I/O, parsowanie lub fallbacki |
| **`tests`** | [`audit-tests`] | Gdy zmieniono logikę biznesową lub pliki testowe (`tests/`, `test_*.py`) |
| **`types`** | [`audit-types`] | Gdy dodano lub zmodyfikowano klasy, `@dataclass`, modele Pydantic, `Enum` lub kontrakty API |
| **`comments`** | [`audit-comments`] | Gdy dodano/zmieniono docstringi, komentarze lub zmieniono sygnatury udokumentowanych funkcji |
| **`security`** | [`audit-security`] | Gdy kod dotyka ścieżek plików, procesów, SQL/wyrażeń, sieci, deserializacji lub uprawnień |
| **`quality`** | [`code-quality`] | Na żądanie (`/audit quality`) lub przy dużych klasach/modułach (DRY, SOLID, ortogonalność) |
| **`perf`** | [`leetcode-style-code-reviewer`] | Na żądanie (`/audit perf`) lub przy pętlach numerycznych / NumPy / Pandas / hot-path |
| **`all`** *(domyślny)* | Wszystkie pasujące aspekty z powyższej tabeli | Automatycznie dobiera aspekty na podstawie zawartości diffu (`git diff --name-only` + treść hunków) |

---

## 2. Proces Wykonania (Adaptacyjny)

1. **Ustal zakres i pasujące aspekty:**
   - Sprawdź zmienione pliki (`git diff @{upstream}...HEAD` + `git diff HEAD`, lub wskazany plik/katalog/PR).
   - Jeśli użytkownik podał konkretne aspekty (np. `/audit errors types` albo `/audit security`), przeczytaj odpowiednie pliki `SKILL.md` z tabeli powyżej i wykonaj dokładnie te audyty.
   - Jeśli użytkownik wpisał samo `/audit` lub `/audit all`, sprawdź jakie elementy zawiera diff (czy są nowe typy? czy jest obsługa błędów? czy są testy? czy są docstringi?) i wybierz wszystkie pasujące aspekty.
2. **Tryb wykonania (Inline vs. Subagenty):**
   - **Mały/średni zakres (1–2 aspekty lub diff < ~200 linii):** Przeczytaj instrukcje wybranych skilli i wykonaj audyty sekwencyjnie w bieżącym kontekście.
   - **Duży zakres (3+ aspektów na dużym diffie / całym module lub z flagą `--parallel`):** Uruchom równoległe subagenty `research` przez `invoke_subagent` (po jednym na każdy wybrany aspekt audytu), przekazując każdemu pełne wytyczne danego skilla.
3. **Skonsolidowany Raport Końcowy:**
   Połącz wyniki ze wszystkich uruchomionych aspektów w jeden przejrzysty raport pogrupowany według priorytetu (`CRITICAL` → `HIGH` → `MEDIUM`), z klikalnymi linkami `[plik.ext:L10-L20](file:///...)` i konkretnymi rekomendacjami kodu. Jeśli podano flagę `--fix`, od razu wdróż jednoznaczne poprawki.
