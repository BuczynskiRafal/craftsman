---
name: batch
description: >-
  Planowanie i równoległe wykonywanie masowych, powtarzalnych zmian w wielu
  plikach lub modułach (na bazie wbudowanego skilla /batch z Claude Code
  2.1.287). Dzieli dużą migrację lub refaktor na niezależne jednostki pracy
  (Work Units), ustala przepis weryfikacji, a po zatwierdzeniu planu uruchamia
  równoległe subagenty i śledzi postęp w tabeli. Używaj po wywołaniu /batch.
---

# Batch Parallel Refactor & Migration (`/batch`)

Planujesz i przeprowadzasz szeroko zakrojone, mechaniczne lub strukturalne zmiany w wielu plikach repozytorium (np. migracje API, masowa aktualizacja typowania, ujednolicenie obsługi błędów, zmiana sygnatur w dziesiątkach modułów), rozbijając je na niezależne, równoległe jednostki pracy (*Work Units*).

Komunikuj się **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `file://`.

---

## Phase 1 — Research & Plan (Badanie i Dekompozycja)

1. **Zbadaj zakres zmiany:**
   Przeszukaj repozytorium (`grep_search` / `find_by_name`, lub przy bardzo dużym repozytorium za pomocą subagentów `research`), aby znaleźć wszystkie pliki i miejsca objęte instrukcją użytkownika.
2. **Podziel pracę na niezależne jednostki (*Work Units*):**
   - Zgrupuj pliki w logiczne, **rozłączne** pakiety (tak, aby dwa subagenty nie edytowały tego samego pliku naraz!).
   - Dla każdej jednostki określ: krótki tytuł, dokładną listę plików oraz jednozdaniowy opis zmiany.
3. **Ustal przepis weryfikacji (*Verification Recipe*):**
   - Określ krótką, konkretną komendę weryfikującą poprawność pojedynczej jednostki (np. `pytest tests/unit/test_xyz.py`, `ruff check <pliki>`, `mypy <pliki>`).
4. **Przedstaw plan do akceptacji:**
   Wypisz czytelny plan zawierający:
   - podsumowanie liczby znalezionych miejsc i plików,
   - ponumerowaną tabelę jednostek pracy (*Work Units*),
   - przepis weryfikacyjny,
   i zapytaj użytkownika, czy uruchomić wykonanie.

---

## Phase 2 — Spawn Workers (Równoległe Wykonanie)

Po zatwierdzeniu planu przez użytkownika:
- Uruchom równoległe subagenty (`invoke_subagent`, `TypeName: "self"`, `Model: "inherit"`).
  - Jeśli jednostki mają pracować na wspólnych plikach bez konfliktów, rozdziel pliki rozłącznie między subagenty w bieżącym workspace (`Workspace: "inherit"`) lub użyj izolowanego worktree (`Workspace: "share"`), jeśli każda jednostka ma stworzyć osobną gałąź.
- Każdy subagent musi otrzymać w pełni samodzielny prompt zawierający:
  1. Cel nadrzędny zmiany,
  2. Dokładną listę przypisanych mu plików (**zakaz edycji plików spoza swojej listy!**),
  3. Konwencje projektu, których musi przestrzegać,
  4. Komendę weryfikacyjną, którą musi uruchomić przed zakończeniem pracy.

---

## Phase 3 — Track Progress & Final Verification (Śledzenie i Domknięcie)

1. Po uruchomieniu i zakończeniu pracy subagentów zaprezentuj tabelę statusu:

```markdown
| # | Jednostka pracy (Work Unit) | Pliki | Status | Weryfikacja |
| :-: | :--- | :--- | :---: | :--- |
| 1 | `<tytuł jednostki 1>` | `<pliki>` | ✅ Done / ❌ Failed | `<wynik testu>` |
| 2 | `<tytuł jednostki 2>` | `<pliki>` | ✅ Done / ❌ Failed | `<wynik testu>` |
```

2. Na koniec uruchom zbiorczą weryfikację na całym drzewie roboczym (`git status`, `git diff --stat`, testy / linter), aby potwierdzić pełną spójność między wszystkimi jednostkami.
