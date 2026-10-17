---
name: feature-dev
description: >-
  Ustrukturyzowany, 7-fazowy proces projektowania i wdrażania nowej
  funkcjonalności z głębokim zrozumieniem istniejącej architektury (na bazie
  pluginu feature-dev z Claude Code: code-explorer -> Clarifying Questions ->
  code-architect -> Implementation -> Quality Review). Używaj po wywołaniu
  /feature-dev lub przy budowie nowych, nietrywialnych funkcji.
---

# Guided Feature Development (`/feature-dev`)

Prowadzisz użytkownika przez zdyscyplinowany, 7-fazowy proces budowy nowej funkcjonalności oparty na architekturze `feature-dev` z Claude Code. Zamiast zgadywać wymagania i od razu pisać kod, najpierw dogłębnie poznajesz istniejące wzorce w repozytorium, wyjaśniasz wszystkie niejednoznaczności, prezentujesz warianty architektoniczne i dopiero po akceptacji wdrażasz rozwiązanie.

Komunikuj się **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `file://`.

---

## Zasady Nadrzędne

- **Understand before acting:** Zawsze najpierw zbadaj istniejące konwencje i abstrakcje w projekcie.
- **Ask clarifying questions early:** Zadaj wszystkie pytania o niedopowiedziane detale i przypadki brzegowe **po** zbadaniu kodu (Faza 2), ale **przed** projektowaniem architektury (Faza 4). Używaj narzędzia `ask_question` do wielokrotnych wyborów architektonicznych i decyzyjnych.
- **Tryb adaptacyjny:** Dla mniejszych funkcji wykonuj eksplorację bezpośrednio; dla rozbudowanych funkcjonalności obejmujących wiele modułów uruchom równoległe subagenty `research` (`invoke_subagent`).

---

## 7 Faz Procesu `/feature-dev`

### Phase 1: Discovery (Zrozumienie celu)
1. Jeśli cel biznesowy/inżynierski nowej funkcji jest niejasny na starcie, dopytaj krótko: jaki problem rozwiązujemy, jakie ma być zachowanie docelowe i jakie są ograniczenia.
2. Podsumuj w 2–3 zdaniach cel zadania i przejdź od razu do eksploracji kodu.

### Phase 2: Codebase Exploration (`code-explorer`)
**Cel:** Zrozumieć istniejący kod, konwencje i punkty rozszerzeń na wysokim i niskim poziomie.
1. Przeszukaj repozytorium (bezpośrednio lub uruchamiając 2–3 równoległe subagenty `research` przez `invoke_subagent`) pod kątem:
   - analogicznych funkcji już zaimplementowanych w projekcie,
   - warstw architektury, przepływu sterowania i modeli danych,
   - konwencji obsługi błędów, UI/CLI oraz podejścia do testów.
2. Przeczytaj kluczowe zidentyfikowane pliki (`view_file`).
3. Przedstaw zwięzłe podsumowanie odkrytych wzorców i wskazanie najważniejszych plików (`[plik.py](file:///...)`), w które wpisze się nowa funkcja.

### Phase 3: Clarifying Questions (Wyjaśnienie niejednoznaczności)
**KRYTYCZNA FAZA — NIE POMIJAJ JEJ.**
1. Na podstawie analizy kodu z Fazy 2 wypunktuj wszystkie niedopowiedziane kwestie:
   - warunki brzegowe (*edge cases*),
   - zachowanie w przypadku błędnych danych / braku zasobów,
   - punkty integracji z istniejącymi modułami i kompatybilność wsteczna,
   - granice zakresu (*co robimy teraz, a czego nie*).
2. Zadaj pytania użytkownikowi (użyj narzędzia `ask_question` z rekomendowanymi opcjami `(Recommended)` na pierwszym miejscu) i **zaczekaj na odpowiedź przed przejściem do Fazy 4**.

### Phase 4: Architecture Design (`code-architect`)
**Cel:** Zaprojektować i porównać podejścia implementacyjne.
1. Opracuj **2–3 warianty architektoniczne**:
   - **Wariant A (Minimal changes):** najmniejsza ingerencja, maksymalne reużycie istniejącego kodu,
   - **Wariant B (Clean architecture):** najczystsze rozdzielenie odpowiedzialności, łatwa rozszerzalność i testowalność,
   - **Wariant C (Pragmatic balance):** złoty środek między prostotą a czystością architektury.
2. Przedstaw krótkie porównanie trade-offów, konkretne pliki do utworzenia/modyfikacji oraz **Twoją wyraźną rekomendację z uzasadnieniem**.
3. Zapytaj użytkownika o zatwierdzenie wybranego wariantu.

### Phase 5: Implementation (Wdrożenie)
**NIE ROZPOCZYNAJ BEZ ZATWIERDZENIA ARCHITEKTURY W FAZIE 4.**
1. Zaimplementuj funkcjonalność ściśle według wybranego projektu i konwencji repozytorium (`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`).
2. Dopisz testy weryfikujące nową logikę i warunki brzegowe ustalone w Fazie 3.

### Phase 6: Quality Review (`/simplify` + `/code-review` + `/verify`)
1. Przejrzyj właśnie napisany kod pod kątem:
   - prostoty, DRY i właściwego poziomu abstrakcji (*Altitude*),
   - poprawności funkcjonalnej i braku *silent failures*,
   - uruchomienia testów i przebiegu runtime (`/verify`).
2. Napraw wykryte usterki od ręki lub przedstaw użytkownikowi kwestie wymagające decyzji.

### Phase 7: Summary (Podsumowanie)
Zakończ krótkim raportem:
- co zostało zbudowane,
- kluczowe decyzje architektoniczne,
- lista zmodyfikowanych i nowych plików z klikalnymi linkami `file://`,
- wynik weryfikacji testowej.
