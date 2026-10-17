---
name: verify
description: >-
  Weryfikacja działania zmian w kodzie end-to-end w runtime (/verify). Nie
  ogranicza się do uruchomienia testów jednostkowych czy lintera — faktycznie
  uruchamia zmodyfikowany przepływ (CLI, skrypt, serwer, pipeline danych,
  headless GUI/QGIS/FreeCAD) i obserwuje realne zachowanie oraz wyjście. Używaj
  po wywołaniu /verify lub przed commitem nietrywialnych zmian w kodzie
  produkcyjnym.
---

# Verify (`/verify` — End-to-End Runtime Verification)

Weryfikujesz, czy zmiana w kodzie **faktycznie robi to, do czego została stworzona**, poprzez przećwiczenie jej **end-to-end w runtime** i zaobserwowanie realnego zachowania — a nie tylko przez odpalenie `pytest`, `ruff` czy `mypy`.

> **Kiedy NIE używać:** Jeśli diff dotyka wyłącznie dokumentacji (`*.md`), komentarzy lub samych plików testowych bez żadnej powierzchni uruchomieniowej (*runtime surface*) w kodzie produkcyjnym — poinformuj o tym krótko i zakończ. Każda zmiana w kodzie produkcyjnym ma powierzchnię uruchomieniową, którą należy przetestować.

Raport z weryfikacji formułuj **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `file://`.

---

## Phase 1 — Identyfikacja powierzchni uruchomieniowej (*Runtime Surface*)

1. Sprawdź bieżący diff (`git diff @{upstream}...HEAD` oraz `git diff HEAD`, lub cel wskazany w argumencie `/verify <cel>`).
2. Sprawdź, czy w repozytorium istnieje już dedykowana instrukcja lub skrypt weryfikacyjny (w `GEMINI.md`, `AGENTS.md`, `CLAUDE.md`, `Makefile`, `pyproject.toml`, `package.json`).
3. Określ typ powierzchni uruchomieniowej dotkniętej zmianą:
   - **Narzędzie CLI / Skrypt / Pipeline naukowy (np. SWMM, GIS):** jakie polecenie z jakimi flagami i danymi wejściowymi przechodzi dokładnie przez zmieniony kod?
   - **Biblioteka / Moduł obliczeniowy:** jaki minimalny skrypt *smoke-test* (`python3 -c "..."`) wywołuje zmienione API na realistycznych danych (w tym na warunkach brzegowych z diffu) i sprawdza niezmienniki wyjścia?
   - **Serwer / API / Web:** uruchomienie serwera i wykonanie żądania (`curl` / klient testowy) uderzającego w zmieniony endpoint oraz sprawdzenie statusu HTTP, payloadu i logów.
   - **Wtyczka Desktop / CAD / GIS (QGIS / FreeCAD / Qt):** sprawdzenie ładowania modułu bez błędów importu, wywołanie logiki kontrolera/sesji w trybie *headless* lub uruchomienie dedykowanego skryptu weryfikującego paczkę.

---

## Phase 2 — Wykonanie weryfikacji wielowarstwowej

Przeprowadź weryfikację od najszybszej warstwy statycznej do pełnego przebiegu runtime:

1. **Warstwa 1: Statyka i kompilacja / importy**
   - Sprawdź, czy zmienione moduły importują się czysto w środowisku projektu (brak `SyntaxError`, `ImportError`, cyklicznych importów, błędów typowania).
2. **Warstwa 2: Testy automatyczne powiązane ze zmianą**
   - Uruchom testy pokrywające zmieniony moduł (np. `pytest <sciezka_testu>`).
3. **Warstwa 3: Przebieg End-to-End (Kluczowy krok `/verify`!)**
   - **Przećwicz dotknięty przepływ na żywo** za pomocą `run_command`:
     - Przekaż realistyczne dane wejściowe, które zmuszają program do wykonania **dokładnie tych gałęzi kodu (`if/else`, nowych metod, zrefaktorowanych funkcji)**, które zostały zmienione w diffie.
     - Przetestuj co najmniej **1 przypadek główny (*happy path*)** oraz **1 przypadek brzegowy / błędny (*edge/error case*)** wynikający ze zmiany.
     - Zaobserwuj rzeczywiste wyjście (`stdout`, `stderr`, kod wyjścia, wygenerowane pliki/artefakty) i sprawdź, czy nie pojawiły się ukryte ostrzeżenia, wyjątki lub błędne wartości liczbowe.
   - Posprzątaj po sobie wszelkie pliki tymczasowe wygenerowane podczas weryfikacji.

---

## Phase 3 — Werdykt i raport

Zakończ zwięzłym raportem:

```markdown
### Weryfikacja Runtime (`/verify`)
- **Cel / Zakres:** `<opis sprawdzonych zmian>`
- **Status:** ✅ **VERIFIED** / ❌ **FAILED** / ⚠️ **PARTIAL**

#### Przeprowadzone próby:
1. **Import & Statyka:** `<komenda>` → `<wynik>`
2. **Testy jednostkowe/integracyjne:** `<komenda>` → `<wynik>`
3. **Przebieg End-to-End (Runtime):**
   - **Wywołanie:** `<dokładna komenda / scenariusz>`
   - **Zaobserwowane zachowanie:** `<konkretny wynik, wartości wyjściowe, logi>`
```

Jeśli podczas `/verify` wykryjesz błąd w działaniu:
- Wskaż dokładną przyczynę i linię kodu (`[plik.ext:L10-L20](file:///...)`),
- Jeśli naprawa jest oczywista i mieści się w zakresie zmian, napraw ją i powtórz przebieg weryfikacyjny, aż uzyskasz `✅ VERIFIED`.
