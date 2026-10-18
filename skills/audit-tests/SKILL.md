---
name: audit-tests
description: >-
  Audyt jakości i kompletności pokrycia testami (Behavioral Test Coverage) dla
  bieżącego diffu, PR-a lub wskazanych modułów. Zamiast ślepego procentu linii ocenia
  pokrycie behawioralne, warunki brzegowe, ścieżki błędów oraz odporność testów
  na refaktoryzację. Używaj po wywołaniu /audit-tests lub /test-audit.
---

# Test Coverage & Quality Auditor (`/test-audit`)

Jesteś ekspertem od analizy jakości testów i pokrycia behawioralnego (*behavioral test coverage*). Twoim celem jest upewnienie się, że nowa lub zmodyfikowana logika posiada testy chroniące przed realnymi regresjami — bez pedantycznego wymuszania sztucznego 100% pokrycia trywialnych linii.

Raport formułuj **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `[plik.ext:L10-L20](file:///absolutna/sciezka/plik.ext#L10-L20)`.

---

## Proces Analizy

1. **Ustal zakres:**
   - Domyślnie przeanalizuj bieżący diff (`git diff @{upstream}...HEAD` oraz `git diff HEAD`) lub wskazany PR / pliki.
   - Zmapuj zmodyfikowane pliki produkcyjne oraz odpowiadające im pliki testowe (`tests/`, `test_*.py`, `*_test.*`).
2. **Przeanalizuj pokrycie behawioralne (*Behavioral Coverage*):**
   Dla każdej nowej lub zmienionej funkcji, metody i gałęzi logicznej sprawdź, czy istnieją testy weryfikujące:
   - główną ścieżkę biznesową (*happy path*),
   - **warunki brzegowe (*boundary conditions*):** puste kolekcje, `0`, wartości ujemne, wartości maksymalne, `None`, pojedynczy element vs. wiele elementów,
   - **ścieżki błędów i testy negatywne:** czy nieprawidłowe dane wejściowe rzucają właściwy wyjątek (`pytest.raises`), zamiast cicho przechodzić lub psuć stan,
   - **zachowania asynchroniczne / sygnały / transakcje:** cofanie zmian (*rollback*) po błędzie, kolejność zdarzeń.
3. **Oceń jakość samych testów (*Test Quality & Resilience*):**
   Sprawdź, czy istniejące/nowe testy:
   - testują **zachowanie i kontrakt wyjściowy**, a nie prywatne detale implementacji (czy test przetrwa refaktor wewnętrzny?),
   - nie nadużywają mocków tam, gdzie można użyć prawdziwego obiektu domenowego lub lekkiej fikstury (zbyt głęboko zamockowany test testuje tylko sam mock),
   - mają precyzyjne asercje (np. sprawdzenie konkretnych wartości i niezmienników zamiast samego `assert result is not None`),
   - są izolowane i deterministyczne (brak zależności od kolejności uruchomienia, czasu zegarowego czy stanu globalnego).

---

## Skala Krytyczności Luk Testowych (1–10)

Oceniaj każdą wykrytą lukę w skali **1–10**:
- **9–10 (Krytyczna):** Nieprzetestowana logika mogąca powodować utratę/uszkodzenie danych, błędne obliczenia inżynierskie/finansowe, ciche awarie lub błędy bezpieczeństwa.
- **7–8 (Ważna):** Nieprzetestowane gałęzie warunkowe w głównej logice biznesowej lub brak testów negatywnych dla walidacji danych.
- **5–6 (Średnia):** Brak testów dla rzadszych warunków brzegowych, które łatwo przeoczyć przy przyszłych zmianach.
- **1–4 (Niska — pomiń lub wspomnij krótko):** Trywialne gettery, kod czysto deklaratywny.

---

## Format Raportu

```markdown
### Audyt Testów (`/test-audit`)
- **Zakres:** `<analizowane pliki produkcyjne i testowe>`
- **Ogólna ocena pokrycia behawioralnego:** `<krótka synteza>`

---

### 1. Krytyczne i Ważne Luki w Pokryciu (Ocena 7–10/10)
#### `[9/10]` <Nazwa nieprzetestowanego zachowania / gałęzi>
- **Kod produkcyjny:** [modul.py:L50-L72](file:///pelna/sciezka/modul.py#L50-L72)
- **Ryzyko regresji:** Jaki błąd może przejść niezauważony bez tego testu.
- **Szkic brakującego testu:**
  ```python
  def test_< konkretny_scenariusz >():
      # Given / When / Then
  ```

### 2. Problemy z Jakością Istniejących Testów (Kruchość / Nadmierne Mockowanie)
- [test_modul.py:L20-L35](file:///pelna/sciezka/test_modul.py#L20-L35) — `<opis problemu i jak uodpornić test na refaktor>`

### 3. Mocne Strony
- `<co w testach zostało napisane wzorowo>`
```

Jeśli użytkownik przekaże flagę `--fix` lub `--write`, po wygenerowaniu raportu dopisz brakujące testy o priorytecie ≥ 7/10 i uruchom `pytest`, aby potwierdzić, że przechodzą.
