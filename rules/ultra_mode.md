# Tryby Głębokiej Pracy Inżynierskiej (`ultrathink` & `ultracode`)

Gdy użytkownik użyje w swojej wiadomości (w dowolnym miejscu zdania) słowa-klucza **`ultrathink`** lub **`ultracode`**, bezwzględnie aktywuj odpowiedni protokół pracy w bieżącej turze:

## 1. Protokół `ultrathink` (Maksymalna głębia rozumowania i architektury)
Słowo `ultrathink` oznacza żądanie najgłębszej możliwej analizy inżynierskiej przed podjęciem decyzji lub napisaniem kodu.
1. **Zakaz dróg na skróty:** Nie generuj od razu powierzchownego rozwiązania ani pierwszego lepszego kodu.
2. **Jawna dekonstrukcja problemu:** Zanim przejdziesz do implementacji, rozpisz zwięźle:
   - **Założenia i niezmienniki (*Invariants*):** co musi pozostać prawdziwe przed i po wykonaniu operacji,
   - **Przypadki brzegowe i awarie (*Edge Cases & Failure Modes*):** puste dane, wartości graniczne (`0`, `None`, `NaN`), współbieżność, błędy I/O, nieprawidłowe stany,
   - **Złożoność obliczeniowa i pamięciowa:** koszt czasowy i pamięciowy na ścieżkach krytycznych (*hot paths*),
   - **Porównanie wariantów (Trade-offs):** rozważ co najmniej 2 podejścia architektoniczne i uzasadnij wybór najlepszego na właściwym poziomie abstrakcji (*Altitude*).

## 2. Protokół `ultracode` (Orkiestracja wieloagentowa i pełna pętla weryfikacji)
Słowo `ultracode` oznacza optymalizację pod kątem najbardziej wyczerpującego, bezbłędnego i zweryfikowanego rozwiązania — koszt tokenów i czas nie są ograniczeniem (z zachowaniem higieny dysku: używaj subagentów w pamięci przez `invoke_subagent`, nie twórz setek plików tymczasowych na dysku).
1. **Faza 1 — Rozpoznanie (`research` subagents):** Dla każdego nietrywialnego zadania uruchom równoległe subagenty `research` (`invoke_subagent`), aby dokładnie zmapować istniejące wzorce w codebase, wywołujących (*call sites*) i potencjalne pułapki.
2. **Faza 2 — Implementacja o wysokiej precyzji:** Wdróż rozwiązanie zgodnie ze standardami projektu i zasadami czystego kodu.
3. **Faza 3 — Obowiązkowa Trójca Jakości przed oddaniem wyniku:**
   Po zakończeniu zmian w kodzie (zanim zgłosisz gotowość użytkownikowi) wykonaj kolejno procedury:
   - **`/simplify`** — sprawdź zmiany pod kątem *Reuse*, *Simplification*, *Efficiency* i *Altitude* oraz wyczyść kod,
   - **`/code-review`** — przeskanuj diff pod kątem błędów poprawności (*Angles A–E*) i napraw wykryte problemy (`CONFIRMED` / `PLAUSIBLE`),
   - **`/verify`** — uruchom zmieniony przepływ end-to-end (testy + realne wywołanie runtime), aby potwierdzić, że zmiana faktycznie działa.
