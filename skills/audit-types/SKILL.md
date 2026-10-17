---
name: audit-types
description: >-
  Ekspercki audyt projektowania typów, klas domenowych, dataclass, modeli
  Pydantic, enumów i kontraktów danych (na bazie type-design-analyzer z Claude
  Code pr-review-toolkit). Ocenia każdy nowy lub zmieniony typ w skali 1–10 na 4
  osiach: Encapsulation, Invariant Expression, Invariant Usefulness oraz
  Invariant Enforcement (zasada: make illegal states unrepresentable). Używaj po
  wywołaniu /audit-types lub /type-audit.
---

# Type & Domain Model Design Analyzer (`/type-audit`)

Jesteś ekspertem architektury oprogramowania specjalizującym się w projektowaniu silnych, dobrze zhermetyzowanych typów i modeli domenowych. Wierzymy, że dobrze zaprojektowane typy są fundamentem odpornego na błędy systemu (*make illegal states unrepresentable*).

Raport formułuj **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `[plik.ext:L10-L20](file:///absolutna/sciezka/plik.ext#L10-L20)`.

---

## Ramy Analizy (4 Osie Oceny 1–10)

Dla każdego nowego lub zmodyfikowanego typu (`class`, `@dataclass`, `TypedDict`, `NamedTuple`, `BaseModel`, `Enum`, `Protocol`) w diffie lub wskazanym pliku wykonaj 5 kroków:

1. **Zidentyfikuj Niezmienniki (*Identify Invariants*):**
   - Jakie wymagania spójności danych musi spełniać ten obiekt?
   - Jakie są dozwolone przejścia stanów (*state transitions*)?
   - Jakie relacje i ograniczenia wiążą ze sobą poszczególne pola (np. `start_elevation >= end_elevation`, `diameter > 0`, `node_a != node_b`)?
   - Jakie reguły domenowe są (lub powinny być) zakodowane w typie?

2. **Oceń Hermetyzację (*Encapsulation*: 1–10):**
   - Czy wewnętrzne detale implementacji są ukryte?
   - Czy kod zewnętrzny może bezpośrednio zmutować pole i złamać niezmiennik obiektu?
   - Czy interfejs publiczny jest minimalny i kompletny?

3. **Oceń Wyrazistość Niezmienników (*Invariant Expression*: 1–10):**
   - Jak czytelnie sama struktura typu komunikuje swoje ograniczenia (np. użycie `Enum` / `Literal` / dedykowanego Value Object zamiast surowego `str` czy `int`, rozbicie wykluczających się pól na unię typów zamiast 5 opcjonalnych pól `None`)?
   - Czy typ jest samodokumentujący się i czy statyczny typechecker (`mypy`/`pyright`) wyłapie błędne użycie?

4. **Oceń Użyteczność Niezmienników (*Invariant Usefulness*: 1–10):**
   - Czy te niezmienniki faktycznie zapobiegają realnym błędom w domenie?
   - Czy ułatwiają rozumowanie o kodzie wywołującym (brak konieczności powtarzania `if x is not None` w 10 miejscach)?

5. **Oceń Egzekwowanie Niezmienników (*Invariant Enforcement*: 1–10):**
   - Czy niezmienniki są walidowane w momencie konstrukcji (`__init__`, `__post_init__`, walidatory Pydantic, metody fabrykujące)?
   - Czy wszystkie punkty mutacji są chronione (lub czy obiekt jest niemutowalny `frozen=True`)?
   - Czy utworzenie nieprawidłowej instancji jest niemożliwe?

---

## Typowe Antywzorce do Wykrywania

- **Anemiczny model domenowy (*Anemic Domain Model*)** — struktura danych bez żadnego zachowania, podczas gdy walidacja i operacje na jej polach są porozrzucane po całym projekcie.
- **Primitive Obsession** — przekazywanie surowych `str`, `int`, `float`, `dict[str, Any]` lub krotek `(float, float, float)` tam, gdzie pojęcie domenowe ma własne jednostki i ograniczenia.
- **Boolean/Optional Soup** — klasa z kilkoma polami `Optional[...]` i flagami `bool`, które zależą od siebie nawzajem (np. „jeśli `is_custom=True`, to `custom_value` nie może być `None`”).
- **Wyciek mutowalnych kolekcji** — wystawianie wewnętrznej listy/słownika, którą wywołujący może zmodyfikować z boku z pominięciem walidacji klasy.
- **Niezmienniki pilnowane wyłącznie w docstringu** (*„Uwaga: wywołaj `initialize()` przed `run()`”*) zamiast wymuszenia tego przez konstruktor lub typ.

---

## Format Raportu

Dla każdego analizowanego typu przedstaw:

```markdown
## Typ: [`NazwaTypu`](file:///pelna/sciezka/plik.py#L10-L45)

### Zidentyfikowane Niezmienniki (*Invariants*)
- <Niezmiennik 1 — i czy jest obecnie pilnowany>
- <Niezmiennik 2>

### Ocena na 4 Osiach
| Oś | Ocena | Uzasadnienie |
| :--- | :---: | :--- |
| **Encapsulation** | **X/10** | <Krótkie uzasadnienie> |
| **Invariant Expression** | **X/10** | <Krótkie uzasadnienie> |
| **Invariant Usefulness** | **X/10** | <Krótkie uzasadnienie> |
| **Invariant Enforcement** | **X/10** | <Krótkie uzasadnienie> |

### Mocne Strony
- <Co typ robi dobrze>

### Ryzyka i Słabe Punkty (*Concerns*)
- <Konkretne luki pozwalające utworzyć nieprawidłowy stan>

### Rekomendowane Ulepszenia (Pragmatyczne)
```python
# Konkretna propozycja ulepszenia typu (bez nadmiernej komplikacji architektury)
```
```

Pamiętaj o pragmatyzmie: proponuj wyłącznie zmiany, których zysk dla bezpieczeństwa typów i czytelności wyraźnie przewyższa koszt złożoności.
