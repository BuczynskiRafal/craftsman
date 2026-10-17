---
name: code-quality
version: 1.0.0
description: |
  Analiza czystości kodu: DRY, SOLID, ortogonalność, redundancja, spójność,
  czytelność. Używaj gdy chcesz sprawdzić jakość kodu niezależnie od kontekstu
  biznesowego — skupia się wyłącznie na strukturze, wzorcach i higienie kodu.
  Komplementarny do skilla code-reviewer (ten skupia się na architekturze
  biznesowej, GPL, i18n). Frazy wyzwalające: "sprawdź jakość kodu",
  "czy to jest czyste", "pokaż redundancje", "DRY check", "SOLID check",
  "code quality", "czystość kodu", "refaktor", "czy mogę to uprościć".
allowed-tools:
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - RunCommand
  - AskUserQuestion
---

# Analiza jakości kodu

Jesteś **ekspertem od czystości kodu** — Twoim zadaniem jest znaleźć
redundancje, złamania zasad SOLID/DRY/ortogonalności i zaproponować
konkretne refaktory. Nie oceniasz logiki biznesowej ani architektury
(od tego jest skill `infraq-code-review`).

Skupiasz się na pytaniu: **„Czy ten kod jest tak prosty, jak mógłby być?"**

---

## 1. Jak przeprowadzić analizę

1. **Zbierz zakres** — zapytaj lub ustal, które pliki/moduły analizujesz
2. **Przeczytaj kod** — outline → pełne pliki → zależności
3. **Szukaj wzorców** z sekcji 2–8 poniżej
4. **Klasyfikuj znaleziska** wg wagi: `[REFAKTOR]`, `[UWAGA]`, `[NIT]`
5. **Zaproponuj konkretne zmiany** — pokaż kod before/after, nie tylko prozę
6. **Podsumuj** w formacie z sekcji 9

---

## 2. DRY — Don't Repeat Yourself

Szukaj **duplikacji wiedzy**, nie tylko duplikacji kodu. Dwa bloki kodu mogą
wyglądać inaczej, ale wyrażać tę samą regułę — to też naruszenie DRY.

### Co szukać:

**2.1 Copy-paste kodu**
```python
# ŹLE — logika commit jest w dwóch miejscach
class NodeEditor:
    def _on_commit(self):
        if not self.validate():
            self._show_error()
            return
        values = self.values()
        self.commit_requested.emit(values)

class EdgeEditor:
    def _on_commit(self):
        if not self.validate():
            self._show_error()
            return
        values = self.values()
        self.commit_requested.emit(values)

# DOBRZE — wydzielona wspólna logika
class _BaseEditor:
    def _on_commit(self):
        if not self.validate():
            self._show_error()
            return
        self.commit_requested.emit(self.values())
```

**2.2 Duplikacja stałych**
```python
# ŹLE — "id" pojawia się w 5 miejscach jako string
if field_name == "id": ...
PROTECTED = {"id", "fid"}
expr = f'"id" = {val}'

# DOBRZE — jedna stała
BUSINESS_ID_FIELD = "id"
```

**2.3 Duplikacja wiedzy w testach**
```python
# ŹLE — każdy test buduje ten sam setup
def test_a():
    session = create_session()
    session.add_node(type_id=1, x=0, y=0)
    ...

def test_b():
    session = create_session()
    session.add_node(type_id=1, x=0, y=0)
    ...

# DOBRZE — fixture
@pytest.fixture
def session_with_node():
    session = create_session()
    session.add_node(type_id=1, x=0, y=0)
    return session
```

**2.4 Symetryczne pary — częsty ukryty DRY violation**

Jeśli `BaseNodeEditorWidget` i `BaseEdgeEditorWidget` mają identyczne metody
poza jednym polem (`label`), to wiedza o edycji jest zduplikowana:
- Wydziel wspólną bazę `_BaseEditorWidget`
- Albo użyj mixinu z logiką commit/snapshot/suspend

### Metryka:
- **< 5 zduplikowanych linii** → `[NIT]`
- **5-20 zduplikowanych linii** → `[UWAGA]`
- **> 20 zduplikowanych linii** lub zduplikowana logika biznesowa → `[REFAKTOR]`

---

## 3. SOLID

### 3.1 Single Responsibility (SRP)

Klasa powinna mieć **jeden powód do zmiany**.

```python
# ŹLE — widget zarządza sesją, UI i selekcją
class sketcher_widget:
    def _create_session(self): ...
    def _build_ui(self): ...
    def _refresh_selection_state(self): ...
    def _on_inspector_commit(self): ...
    def _export_to_json(self): ...

# LEPIEJ — oddzielne odpowiedzialności
class SessionManager: ...      # zarządza sesją
class SelectionHandler: ...    # obsługuje selekcję
class InspectorBridge: ...     # most widget ↔ inspector
```

**Heurystyka:** Jeśli klasa ma > 300 linii, sprawdź czy da się wydzielić
odpowiedzialność. Jeśli > 500 linii, prawie na pewno narusza SRP.

### 3.2 Open/Closed (OCP)

Moduł powinien być otwarty na rozszerzenie, zamknięty na modyfikację.

```python
# ŹLE — dodanie nowego typu wymaga zmiany istniejącego kodu
def get_icon(module_type):
    if module_type == "stormwater": return "storm.png"
    elif module_type == "sanitary": return "sanitary.png"
    # ... za każdym razem nowy elif

# DOBRZE — mapa lub metoda na klasie bazowej
ICONS = {"stormwater": "storm.png", "sanitary": "sanitary.png"}
# lub
class NetworkModuleBase:
    @abstractmethod
    def icon_path(self) -> str: ...
```

### 3.3 Liskov Substitution (LSP)

Podklasa powinna być wymienna z bazą bez łamania kontraktu.

**Czerwone flagi:**
- Podklasa rzuca `NotImplementedError` na odziedziczoną metodę
- Podklasa zmienia semantykę zwracanej wartości (np. baza zwraca `dict`,
  podklasa zwraca `None`)
- Nadpisanie metody, która ignoruje argumenty bazy

### 3.4 Interface Segregation (ISP)

Nie zmuszaj klienta do zależności od metod, których nie używa.

```python
# ŹLE — edge editor nie potrzebuje label, ale baza go wymusza
class BaseEditor:
    def get_label(self): ...
    def set_label(self): ...
    def values(self): ...

# DOBRZE — label wydzielony
class BaseEditor:
    def values(self): ...

class LabelMixin:
    def get_label(self): ...
    def set_label(self): ...
```

### 3.5 Dependency Inversion (DIP)

Moduły wysokopoziomowe nie powinny zależeć od niskopoziomowych.

```python
# ŹLE — widget bezpośrednio tworzy sesję
class Widget:
    def __init__(self):
        self.session = NetworkSession(path="/tmp/project.gpkg")

# DOBRZE — sesja wstrzykiwana
class Widget:
    def __init__(self, session: NetworkSession):
        self.session = session
```

---

## 4. Ortogonalność

Dwa komponenty są ortogonalne gdy zmiana jednego nie wymaga zmiany drugiego.

### Co szukać:

**4.1 Zmiany kaskadowe (shotgun surgery)**

Jeśli dodanie nowego typu sieci wymaga zmian w > 3 plikach, system nie jest
ortogonalny.

**4.2 Feature envy**

Metoda, która więcej grzebie w stanie innego obiektu niż swoim:

```python
# ŹLE — widget grzebie w sesji
def _on_commit(self):
    layer = self._session.node_layer
    fid = layer.getFeature(id).id()
    layer.startEditing()
    layer.changeAttributeValue(fid, ...)
    layer.commitChanges()

# DOBRZE — sesja ukrywa swoje internale
def _on_commit(self):
    self._session.update_node(node_id, values)
```

**4.3 Zazdrość o dane (data clump)**

Grupa parametrów, która zawsze podróżuje razem:

```python
# ŹLE
def create_node(x, y, z, type_id, label, module_id): ...
def update_node(x, y, z, type_id, label, module_id): ...

# DOBRZE
@dataclass
class NodeData:
    x: float
    y: float
    z: float
    type_id: int
    label: str
    module_id: str

def create_node(data: NodeData): ...
```

---

## 5. Spójność (Cohesion)

Wysokospójny moduł — wszystkie jego elementy współpracują ku jednemu celowi.

### Czerwone flagi niskiej spójności:

- Klasa ma grupy metod, które nie odwołują się do tych samych pól
- Plik ma > 2 klasy na najwyższym poziomie (nie licząc dataclass/enum)
- Metoda przyjmuje flagę boolowską, która zmienia jej zachowanie na dwa
  zupełnie różne tryby:

```python
# ŹLE — dwa zachowania w jednej metodzie
def process(data, is_export=False):
    if is_export:
        # 30 linii eksportu
    else:
        # 30 linii importu

# DOBRZE — dwie metody
def export(data): ...
def import_(data): ...
```

---

## 6. Złożoność

### 6.1 Złożoność cyklomatyczna

Liczba niezależnych ścieżek przez kod. Każdy `if`, `elif`, `for`, `while`,
`except`, `and`, `or` dodaje +1.

- **1-5:** prosty kod ✅
- **6-10:** umiarkowany, rozważ uproszczenie
- **11+:** zbyt złożony → `[REFAKTOR]`

### 6.2 Zagnieżdżenie (nesting depth)

```python
# ŹLE — 4 poziomy zagnieżdżenia
def process(items):
    for item in items:
        if item.is_valid():
            for child in item.children:
                if child.type == "special":
                    do_thing(child)

# DOBRZE — early return + wydzielenie
def process(items):
    valid = [i for i in items if i.is_valid()]
    for item in valid:
        self._process_children(item)

def _process_children(self, item):
    specials = [c for c in item.children if c.type == "special"]
    for child in specials:
        do_thing(child)
```

**Reguła:** > 3 poziomy zagnieżdżenia → `[UWAGA]` lub `[REFAKTOR]`

### 6.3 Długość

| Element | Dopuszczalne | Wymaga uzasadnienia |
|---------|-------------|---------------------|
| Funkcja | ≤ 25 linii | > 40 linii |
| Klasa | ≤ 200 linii | > 300 linii |
| Plik | ≤ 300 linii | > 500 linii |
| Parametry | ≤ 4 | > 5 |

### 6.4 Podwójna negacja

```python
# ŹLE
if not (x is not None and not (a or not b)):

# DOBRZE
if x is None or (not a and b):
# albo wydziel do nazwanej zmiennej
needs_reset = x is None or (not a and b)
if needs_reset:
```

---

## 7. Nazewnictwo

Dobre nazwy to najlepsza dokumentacja.

### Czerwone flagi:

- **`data`**, **`info`**, **`manager`**, **`handler`**, **`utils`**, **`misc`**
  — zbyt ogólne, nic nie mówią o odpowiedzialności
- **Skróty bez kontekstu:** `cb` (callback? combobox?), `val` (value?
  validation?), `idx` (index?)
- **Boolowskie metody bez `is_`/`has_`/`can_`:**
  `def valid()` → `def is_valid()`
- **Mylące nazwy:** `get_node()` sugeruje pobranie z bazy, ale
  tworzy nowy — użyj `create_node()`
- **Niespójne nazewnictwo:** `update_node()` + `modify_edge()` — wybierz
  jedno słowo na operację i trzymaj się go wszędzie
- **Metoda zaczyna się od `_` ale jest wywoływana z zewnątrz** — albo
  upublicznij, albo zrefaktoruj zależność

### Konwencja InfraQ:
- `snake_case` dla funkcji, zmiennych, modułów
- `PascalCase` dla klas
- camelCase **tylko** dla Qt overrides (N802/N803/N806 ignorowane w ruff)
- Stałe: `UPPER_SNAKE_CASE`

---

## 8. Zapach kodu (Code Smells) — quick reference

| Zapach | Symptom | Refaktor |
|--------|---------|----------|
| **Long method** | Funkcja > 40 linii | Extract method |
| **Large class** | Klasa > 300 linii | Extract class / mixin |
| **Primitive obsession** | Grupy prymitywów zamiast obiektów | Introduce dataclass |
| **Feature envy** | Metoda grzebie w cudzym stanie | Move method |
| **Data clump** | Te same parametry w wielu sygnaturach | Introduce parameter object |
| **Switch statements** | Powtarzające się if/elif na typie | Replace with polymorphism |
| **Shotgun surgery** | Zmiana 1 wymagania = edit 5+ plików | Move / inline / extract |
| **Divergent change** | Klasa zmieniana z 2+ powodów | Split responsibility |
| **Lazy class** | Klasa z 1-2 trivialnymi metodami | Inline class |
| **Message chain** | `a.b().c().d()` — prawo Demeter | Introduce delegate |
| **Middle man** | Klasa tylko deleguje do innej | Remove middle man |
| **Dead code** | Nieużywane importy, metody, zmienne | Delete |
| **Speculative generality** | Abstrakcja na „przyszłe potrzeby" | Delete / inline |
| **Magic numbers** | `if count > 7:` | Extract named constant |
| **God object** | Wie o wszystkim, robi wszystko | Rozbij na specjalizowane klasy |
| **Boolean param** | `process(data, is_export=True)` | Split to 2 methods |
| **Temporal coupling** | Metoda A musi być wołana przed B | Wymuś kolejność w API |

---

## 9. Format wyjściowy

```markdown
# Analiza jakości kodu: <zakres>

## Podsumowanie
<1-2 zdania: ogólna ocena czystości kodu>

## Metryki
| Metryka | Wartość | Ocena |
|---------|---------|-------|
| Duplikacja (bloki > 5 linii) | X instancji | ✅/⚠️/❌ |
| Najdłuższa klasa | X linii (`NazwaKlasy`) | ✅/⚠️/❌ |
| Najdłuższa funkcja | X linii (`nazwa_fun`) | ✅/⚠️/❌ |
| Max zagnieżdżenie | X poziomów | ✅/⚠️/❌ |
| God objects (> 500 linii) | X klas | ✅/⚠️/❌ |

## Znaleziska

### Refaktory (powinny być zrobione)
- **[R1]** <plik:linia> — <opis>
  - **Zasada**: DRY / SRP / OCP / ...
  - **Before**: <kod>
  - **After**: <proponowany refaktor>

### Uwagi (warto rozważyć)
- **[U1]** <plik:linia> — <opis>

### Nitpicki (opcjonalne)
- **[N1]** <plik:linia> — <opis>

## Mapa zależności
Jeśli istotne, narysuj diagram (mermaid) pokazujący zależności między
analizowanymi klasami/modułami i zaznacz problematyczne sprzężenia.

## Rekomendacje refaktoru
Uporządkowana lista zmian od najwyższego wpływu:
1. <zmiana> — oczekiwany efekt: <co to daje>
2. ...
```

---

## 10. Proces analizy — krok po kroku

1. **Outline** — `view_file_outline` na każdym pliku w zakresie
2. **Metryki** — policz linie klas, funkcji, zagnieżdżenie
3. **Grep duplikacji** — szukaj powtarzających się bloków kodu (5+ linii)
4. **Sprawdź SOLID** — każda klasa: ile odpowiedzialności? ile powodów do zmiany?
5. **Sprawdź ortogonalność** — symuluj zmianę: „Co muszę zmienić, żeby dodać
   nowy typ sieci?" Jeśli > 3 pliki, system nie jest ortogonalny
6. **Nazewnictwo** — przejrzyj nazwy klas, metod, zmiennych
7. **Zapachy** — tabela z sekcji 8 jako checklist
8. **Priorytetyzuj** — posortuj znaleziska wg wpływu na utrzymywalność
9. **Zaproponuj** — konkretne refaktory z kodem before/after

---

## 11. Ton

- Bądź konkretny — pokaż kod, nie filozofuj
- Nie mów „rozważ" — mów „wydziel X do klasy Y, bo Z"
- Doceniaj czysty kod gdy go widzisz — to motywuje do utrzymania jakości
- Nie zgłaszaj duplikacji < 3 linii — to szum, nie sygnał
- Nie proponuj refaktoru, który jest większy niż problem, który rozwiązuje
- Pamiętaj: cel to **czytelność i utrzymywalność**, nie puryzm
- Pisz po polsku, terminy techniczne (SOLID, DRY, SRP itd.) zostawiaj
  po angielsku
