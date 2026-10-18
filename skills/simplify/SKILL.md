---
name: simplify
description: >-
  Oczyszczanie i upraszczanie zmienionego kodu bez zmiany jego zachowania
  (Preserve Functionality). Przegląda diff pod kątem 4 osi inżynierskich
  (Reuse, Simplification, Efficiency, Altitude), a następnie bezpośrednio aplikuje poprawki w plikach. Nie
  szuka błędów poprawności (do tego służy /code-review). Używaj po wywołaniu
  /simplify, /simplyfy lub gdy użytkownik prosi o uproszczenie i wyczyszczenie
  niedawno zmodyfikowanego kodu.
---

# Simplify (`/simplify`)

Podnosisz jakość, czytelność, spójność i wydajność zmienionego kodu **bez zmiany jego działania**. Analizujesz kod pod kątem reużycia (*Reuse*), uproszczenia (*Simplification*), wydajności (*Efficiency*) oraz właściwego poziomu abstrakcji (*Altitude*), a następnie **bezpośrednio wdrażasz poprawki w plikach**. Nie szukasz błędów poprawności — od tego jest `/code-review`.

Podsumowania formułuj **po polsku** z zachowaniem angielskich terminów technicznych. Wszystkie odwołania do plików i symboli podawaj jako klikalne linki Markdown `[plik.ext:L10-L20](file:///absolutna/sciezka/plik.ext#L10-L20)`.

---

## Złote zasady (`/simplify`)

1. **Preserve Functionality (Zachowaj 100% funkcjonalności):** Nigdy nie zmieniaj tego, *co* kod robi — zmieniaj wyłącznie to, *jak* to robi. Wszystkie dotychczasowe zachowania, wyniki i kontrakty API muszą pozostać nienaruszone.
2. **Clarity over Brevity (Przejrzystość ponad zwięzłość):** Wyraźny, czytelny kod jest zawsze lepszy niż przesadnie skondensowane „sprytne” one-linery.
   - **WAŻNE:** Unikaj zagnieżdżonych operatorów trójargumentowych (*nested ternary operators*) — zastępuj je czytelnymi łańcuchami `if/else`, `match/switch` lub funkcją pomocniczą z *early return*.
3. **Maintain Balance (Zachowaj balans — unikaj nadmiernego upraszczania):**
   - Nie łącz zbyt wielu różnych odpowiedzialności w jedną funkcję lub klasę.
   - Nie usuwaj pomocnych abstrakcji, które poprawiają organizację i czytelność modułu.
   - Nie optymalizuj „na mniejszą liczbę linii” kosztem łatwości debugowania i rozszerzania.
4. **Apply Project Standards:** Przestrzegaj reguł zapisanych w `GEMINI.md`, `AGENTS.md`, `.agents/rules/*.md` oraz `CLAUDE.md` (konwencje nazewnictwa, typowanie, sortowanie importów, obsługa błędów).

---

## Phase 0 — Pobranie diffu (Zakres pracy)

1. Uruchom `git diff @{upstream}...HEAD` (lub `git diff main...HEAD` / `git diff HEAD~1`, jeśli brak upstreamu), aby pobrać zatwierdzony diff na bieżącej gałęzi.
2. Jeśli w drzewie roboczym są niezatwierdzone zmiany lub diff względem gałęzi jest pusty, uruchom również `git diff HEAD` i włącz bieżące zmiany robocze do zakresu — `/simplify` jest najczęściej uruchamiane tuż przed commitem.
3. Jeśli użytkownik przekazał jako argument konkretną ścieżkę pliku/katalogu, nazwę gałęzi lub PR, potraktuj wskazany cel jako zakres pracy. Skupiaj się wyłącznie na kodzie niedawno zmodyfikowanym lub jawnie wskazanym przez użytkownika.

---

## Phase 1 — Przegląd pod kątem 4 kątów czyszczenia kodu

### Tryb wykonania (Adaptacyjny)
- **Single-Pass Inline (domyślnie dla małych i średnich zmian < ~200 linii diffu):** Przejdź samodzielnie przez wszystkie **4 kąty** poniżej, po kolei, w bieżącym kontekście — nie pomijaj żadnego kąta.
- **4-Agent Parallel Fan-Out (dla dużych diffów ≥ ~200 linii lub z flagą `--parallel`):** Uruchom **4 niezależne subagenty `research`** za pomocą narzędzia `invoke_subagent` w jednym wywołaniu, aby działały współbieżnie. Przekaż każdemu subagentowi diff oraz jeden z czterech kątów poniżej.

Dla każdego kąta odnotuj znaleziska zawierające: `file`, `line`, jednozdaniowe `summary` oraz **konkretny koszt** (co jest zduplikowane, zmarnowane lub trudniejsze w utrzymaniu).

### 1. Reuse (Reużycie istniejącego kodu)
Wskaż nowy kod, który reimplementuje coś, co codebase już posiada — przeszukaj (`grep_search`) moduły współdzielone/narzędziowe (`utils`, `helpers`, `common`, `shared`) oraz pliki sąsiadujące ze zmianą i wskaż istniejący helper lub klasę, którą należy wywołać zamiast duplikować logikę.

### 2. Simplification (Uproszczenie struktury)
Wskaż zbędną złożoność, którą wprowadza diff:
- nadmiarowy lub wyliczalny stan (*redundant or derivable state*),
- copy-paste z niewielką wariacją,
- głębokie zagnieżdżenia (*deep nesting* — spłaszcz przez *guard clauses* / *early return*),
- pozostawiony martwy kod, nieużywane zmienne lub importy,
- zagnieżdżone operatory trójargumentowe (*nested ternaries*),
- zbędne komentarze opisujące oczywisty kod (zachowaj jednak wszystkie istotne docstringi i komentarze wyjaśniające kontekst/dlaczego — zgodnie z zasadą *documentation integrity*).
Wskaż prostszą formę wykonującą dokładnie to samo zadanie.

### 3. Efficiency (Wydajność i brak marnotrawstwa)
Wskaż marnowaną pracę, którą wprowadza diff:
- powtarzane obliczenia lub wielokrotne operacje I/O / zapytania,
- niezależne operacje wykonywane sekwencyjnie,
- operacje blokujące dodane do ścieżek startowych lub *hot paths*,
- długo żyjące obiekty budowane z domknięć (*closures*) lub przechwyconych środowisk — utrzymują one cały otaczający scope w pamięci przez cały czas życia obiektu (wyciek pamięci, gdy scope przechowuje duże wartości); preferuj klasę, strukturę lub `dataclass`, która kopiuje wyłącznie potrzebne pola.
Wskaż tańszą alternatywę.

### 4. Altitude (Właściwy poziom abstrakcji)
Sprawdź, czy każda zmiana rozwiązuje problem u źródła (*root cause*) na odpowiedniej głębokości architektury, zamiast łatać symptom kruchym obejściem (*bandaid*). Doklejanie przypadków szczególnych (`if/else`) na wspólnej infrastrukturze jest sygnałem, że zmiana jest zbyt płytka — wybierz prostszą, bardziej ogólną zmianę w mechanizmie bazowym i nazwij ją.

---

## Phase 2 — Wdrożenie poprawek (`Apply the fixes`)

1. **Deduplikacja:** Połącz znaleziska z Fazy 1 wskazujące na tę samą linię lub ten sam mechanizm.
2. **Bezpośrednia edycja kodu:** Zaaplikuj każde pozostałe usprawnienie bezpośrednio w plikach za pomocą narzędzia `replace_file_content`.
3. **Bezpieczne pomijanie:** Pomiń każde znalezisko, którego wdrożenie:
   - zmieniłoby zamierzone zachowanie kodu,
   - wymagałoby rozległych zmian daleko poza przeglądanym diffem,
   - oceniasz jako *false positive* lub pogorszenie czytelności.
   Krótko odnotuj powód pominięcia zamiast z nim polemizować.
4. **Podsumowanie końcowe:**
   Zakończ zwięzłym raportem po polsku:
   - tryb wykonania (*Single-Pass Inline* lub *4-Agent Parallel Fan-Out*),
   - **Co zostało uproszczone:** wypunktowana lista wprowadzonych zmian z linkami `[plik.ext:L10-L20](file:///...)`, kategorią (`Reuse` / `Simplification` / `Efficiency` / `Altitude`) i jednym zdaniem o zysku,
   - **Co pominięto:** krótka lista pominiętych sugestii z powodem (albo potwierdzenie, że kod był już czysty i nie wymagał zmian).
