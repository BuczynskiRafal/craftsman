---
name: modernize
description: >-
  Inżynieryjna modernizacja, refaktoryzacja architektoniczna i migracja starego
  kodu (Legacy Code Modernization). Zanim przepisze zawiły moduł, najpierw ekstrahuje z niego ukryte
  reguły biznesowe/obliczeniowe do specyfikacji Given/When/Then (Karty Reguł),
  buduje testy zabezpieczające (characterization tests), a dopiero potem
  wykonuje uplift/transformację i weryfikację równoważności. Używaj po
  wywołaniu /modernize.
---

# Code Modernization & Rule Extraction (`/modernize`)

Prowadzisz bezpieczną modernizację, przebudowę lub migrację złożonego/legacy kodu.

Główna zasada: **Nigdy nie przepisuj starego modułu „z głowy” — najpierw oddziel to, *czego wymaga domena/matematyka*, od tego, *jak stary kod przypadkowo to zaimplementował*.**

Komunikuj się **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `file://`.

---

## Proces Modernizacji (5 Etapów)

### Etap 1: Preflight & Assessment (Inwentaryzacja i Wybór Strategii)
1. Przeanalizuj wskazany moduł/system: rozmiar, zależności wejściowe/wyjściowe, pokrycie testami, wąskie gardła i dług technologiczny.
2. Ustal z użytkownikiem (przez `ask_question`, jeśli nie podano w prompcie) wzorzec modernizacji:
   - **Uplift (Podniesienie wersji / standardu):** ten sam język i architektura, aktualizacja bibliotek/API/typowania (np. migracja Qt5 → Qt6, Python legacy → nowoczesny typowany Python, Pandas → Polars/NumPy),
   - **Transform (Przebudowa modułu):** wydzielenie czystej logiki domenowej z monolitycznego kodu (np. oddzielenie logiki obliczeń od GUI w QGIS/FreeCAD),
   - **Reimagine (Nowa architektura):** zaprojektowanie nowego modułu spełniającego te same reguły domenowe,
   - **Extract Rules Only (Samo wydobycie reguł i zrozumienie kodu):** udokumentowanie ukrytych reguł i algorytmów bez modyfikacji kodu.
3. Ustal niezmienniki kontraktu: czy zachowanie musi być bit-po-bicie identyczne (wraz z dotychczasowymi osobliwościami), czy przy okazji naprawiamy znane błędy?

---

### Etap 2: Business & Domain Rules Extraction (`business-rules-extractor`)
Prześwietl modernizowany kod i wyciągnij wszystkie ukryte w nim **reguły domenowe i obliczeniowe** (pomijając czystą infrastrukturę, UI czy logowanie):
- **Obliczenia i wzory:** formuły matematyczne/hydrologiczne/geometryczne, współczynniki, zaokrąglenia, jednostki,
- **Walidacje i progi (*Thresholds*):** wymagane pola, zakresy wartości, zależności między polami,
- **Przejścia stanów i warunki kwalifikowalności:** co wyzwala zmianę stanu, kolejność operacji,
- **Zaszyte stałe (*Magic numbers*):** wypisz wszystkie zahardkodowane liczby i parametry wraz z ich dokładną lokalizacją (`[plik.py:L10-L20](file:///...)`). *(Uwaga bezpieczeństwa: jeśli natrafisz na zahardkodowane hasła/klucze API, zamaskuj wartość jako `<credential — masked>`).*

Dla każdej istotnej reguły sformułuj zwięzłą **Kartę Reguły (*Rule Card*)** ze specyfikacją **Given / When / Then** na konkretnych wartościach liczbowych oraz poziomem pewności (`High` / `Medium` / `Low` — przy `Low` sformułuj pytanie do użytkownika).

---

### Etap 3: Characterization Tests (Siatka bezpieczeństwa przed zmianą)
Zanim zmodyfikujesz stary kod produkcyjny:
1. Przekształć wyekstrahowane w Etapie 2 reguły `Given / When / Then` w automatyczne testy kontraktowe (*characterization / golden tests*).
2. Uruchom je na **starym kodzie**, aby potwierdzić, że testy przechodzą i zamrażają obecne zachowanie referencyjne.

---

### Etap 4: Transformacja / Uplift (`architecture-critic` & `uplift-migrator`)
1. Wdróż docelową architekturę lub migrację w logicznych, atomowych krokach.
2. Zadbaj o to, by każda wyekstrahowana reguła z Etapu 2 miała swoje czytelne, jawne miejsce w nowym kodzie (a *magic numbers* stały się nazwanymi stałymi lub parametrami konfiguracji).

---

### Etap 5: Weryfikacja Równoważności (`modernize-verify`)
1. Uruchom zestaw testów z Etapu 3 na zmodernizowanym kodzie.
2. Porównaj wyniki (izomorfizm wyjść, wydajność, obsługa błędów).
3. Przedstaw końcową tabelę podsumowującą: lista zachowanych reguł domenowych, wprowadzone zmiany architektoniczne oraz ewentualne świadomie zmienione zachowania.
