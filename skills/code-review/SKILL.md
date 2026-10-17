---
name: code-review
description: >-
  Przegląd kodu (Code Review) bieżącego diffu git, gałęzi, wskazanych plików lub
  Pull Requesta z GitHuba/GitLaba. Wykrywa błędy poprawności (Angles A–E),
  problemy z reużyciem, uproszczeniem, wydajnością, poziomem abstrakcji (Altitude)
  oraz naruszenia reguł projektu (GEMINI.md, AGENTS.md, CLAUDE.md), a następnie
  weryfikuje znaleziska (CONFIRMED / PLAUSIBLE / REFUTED). Obsługuje poziomy
  dokładności (low, medium, high, xhigh, max) oraz flagi (--fix, --comment,
  --parallel). Używaj po wywołaniu /code-review, /review lub gdy użytkownik prosi
  o code review zmian albo PR.
---

# Code Review (`/code-review`)

Przeprowadzasz wielokątowy przegląd kodu oparty na architekturze wbudowanego silnika `/code-review` z Claude Code (v2.1.287) połączonej z audytem Pull Requestów.

Raporty i wyjaśnienia formułuj **po polsku**, zachowując oryginalne angielskie terminy techniczne (`CONFIRMED`, `PLAUSIBLE`, `REFUTED`, `off-by-one`, `falsy-zero`, `call site`, `invariant`, `guard clause`, `hot path`, `altitude` itd.). Wszystkie odwołania do plików i symboli w raporcie muszą być klikalnymi linkami Markdown w formacie `[nazwa_pliku.ext:L10-L20](file:///absolutna/sciezka/do/pliku.ext#L10-L20)`.

---

## Parsowanie argumentów i poziomów dokładności (`effort`)

Przeanalizuj argumenty przekazane po `/code-review`:

1. **Poziom dokładności (`effort`)** — domyślnie **`medium`**:
   - `low` — szybki przegląd w 1 przebiegu tylko po hunkach diffu (bez czytania całych plików, bez subagentów, pomija pliki testów/fixtures, max **4–8** najważniejszych błędów runtime).
   - `medium` *(domyślny)* — przegląd nastawiony na **precyzję (*precision*)**: zgłaszaj wyłącznie problemy, na które maintainer faktycznie zareaguje. 8 kątów analitycznych (Angles A–C + Reuse, Simplification, Efficiency, Altitude, Conventions), weryfikacja, limit do **8** znalezisk.
   - `high` — przegląd nastawiony na **wykrywalność (*recall*)**: wyłapanie każdego realnego błędu jest ważniejsze niż unikanie false-positive'ów. Pełne kąty analityczne + weryfikacja *recall-biased*, limit do **10** znalezisk.
   - `xhigh` / `max` / `ultra` — **maksymalna wykrywalność (*maximum recall*)**: wszystkie 10 kątów (Angles A–E + 4 kąty jakości + Conventions) + weryfikacja + dodatkowa **Faza 3 (Sweep for gaps)**, limit do **15** znalezisk.
2. **Flagi opcjonalne**:
   - `--fix` — po wygenerowaniu i zweryfikowaniu listy znalezisk od razu zaaplikuj poprawki w drzewie roboczym.
   - `--comment` — jeśli celem jest GitHub PR lub GitLab MR, opublikuj wynik przeglądu jako komentarz na PR/MR (`gh` / `glab`).
   - `--parallel` — wymuś wieloagentowy fan-out przez narzędzie `invoke_subagent` nawet dla mniejszego diffu.
   - `--inline` — wymuś wykonanie wszystkich kątów w bieżącym kontekście bez uruchamiania subagentów.
3. **Cel przeglądu (`<target>`)**:
   - Numer lub URL Pull Requesta (np. `#42`, `42`, URL GitHuba/GitLaba) → tryb przeglądu PR.
   - Nazwa gałęzi, zakres commitów lub ścieżka pliku/katalogu → przegląd wskazanego zakresu.
   - Brak celu → lokalny diff w repozytorium.

---

## Tryb orkiestracji (Adaptacyjny: Inline vs. Subagenty)

Dobierz strategię wykonania automatycznie na podstawie rozmiaru diffu i wybranego poziomu:

- **Tryb Single-Pass Inline (w bieżącym kontekście, bez subagentów):**
  Stosuj, gdy poziom to `low`, przekazano `--inline`, narzędzie `invoke_subagent` jest niedostępne **lub** łączny diff ma **mniej niż ~200 zmienionych linii** przy poziomie `medium`. Przejdź przez wszystkie wymagane kąty sekwencyjnie w jednym przebiegu — nie pomijaj żadnego kąta.
- **Tryb Multi-Agent Fan-Out (`invoke_subagent`):**
  Stosuj, gdy poziom to `high` / `xhigh` / `max`, przekazano `--parallel`, przeglądasz duży PR **lub** diff przekracza **~200 zmienionych linii**. Uruchom równoległe subagenty typu `research` (`Model: "inherit"`) w jednym wywołaniu `invoke_subagent`. Skaluj liczbę subagentów szukających do rozmiaru diffu wzorem: `min(8, max(2, ceil(liczba_linii_diffu / 150)))`, rozdzielając między nie kąty analityczne z Fazy 1.

---

## Phase 0 — Pobranie diffu i reguł projektu

1. **Pobranie zakresu zmian:**
   - **Lokalny diff (domyślnie):** uruchom `git diff @{upstream}...HEAD` (lub `git diff main...HEAD` / `git diff HEAD~1`, jeśli brak upstreamu). Jeśli w drzewie roboczym są niezatwierdzone zmiany lub diff względem gałęzi jest pusty, uruchom również `git diff HEAD` i włącz zmiany robocze (staged + unstaged) do zakresu przeglądu.
   - **GitHub PR (gdy podano numer/URL PR):** użyj `gh pr view <pr> --json state,isDraft,title,body,author,headRefOid` oraz `gh pr diff <pr>`. Jeśli PR jest zamknięty lub jest draftem (o ile użytkownik nie poprosił jawnie o przegląd draftu), poinformuj o tym i przerwij.
2. **Odkrycie reguł projektu (Conventions):**
   - Znajdź pliki z regułami obowiązującymi dla zmienianych plików: `GEMINI.md`, `AGENTS.md`, `.agents/rules/*.md`, `CLAUDE.md` oraz `CLAUDE.local.md` w katalogu głównym repozytorium oraz w katalogach nadrzędnych zmienionych plików.

---

## Phase 1 — Wyszukiwanie kandydatów (10 kątów analitycznych)

Przeanalizuj diff pod kątem poniższych kątów. Każdy znaleziony kandydat musi zawierać: `file`, `line`, jednozdaniowe `summary` oraz konkretny `failure_scenario` (lub konkretny koszt utrzymaniowy/wydajnościowy).

> **Ważne:** Przekaż dalej każdego kandydata, dla którego potrafisz nazwać konkretny scenariusz awarii — przedwczesne odrzucanie niepewnych kandydatów w Fazie 1 pomija krok weryfikacji i jest główną przyczyną przeoczonych błędów.

### Kąty poprawności (*Correctness Angles*)

#### Angle A — Line-by-line diff scan
Przeczytaj każdy hunk w diffie linia po linii. Następnie **przeczytaj całą otaczającą funkcję** (`view_file`) dla każdego hunku — błędy w niezmienionych liniach dotkniętej funkcji wchodzą w zakres przeglądu (PR ponownie je eksponuje lub nie naprawia ich). Dla każdej linii zadaj pytanie: *jakie dane wejściowe, stan, timing lub platforma sprawiają, że ta linia zadziała błędnie?*
Szukaj w szczególności:
- odwróconych lub błędnych warunków logicznych,
- błędów brzegowych (*off-by-one*),
- dereferencji `None` / `null` / `undefined`,
- brakującego `await`,
- sprawdzeń typu *falsy-zero* (np. `if not val:` gdy `0`, `0.0` lub `""` jest poprawną wartością),
- użycia złej zmiennej po copy-paste,
- połkniętych wyjątków w `except` / `catch`, które powinny zostać propagowane,
- nieeskejpowanych znaków specjalnych w wyrażeniach regularnych.

#### Angle B — Removed-behavior auditor
Dla każdej linii, którą diff **USUWA lub zastępuje**, nazwij niezmiennik (*invariant*) albo zachowanie, którego ta linia pilnowała, a następnie przeszukaj nowy kod, aby sprawdzić, gdzie ten niezmiennik jest odtworzony. Jeśli nie jest odtworzony, zgłoś kandydata: usunięty guard, porzucona ścieżka obsługi błędu, zawężona walidacja, usunięty test pokrywający realny przypadek.

#### Angle C — Cross-file tracer
Dla każdej funkcji/metody zmienionej w diffie znajdź wszystkie miejsca jej wywołania (`grep_search`) i sprawdź, czy zmiana nie psuje żadnego *call site'u*: nowy warunek wstępny (*precondition*), zmieniony kształt lub typ zwracanej wartości, nowy rzucany wyjątek, zależność od kolejności wywołań. Sprawdź również funkcje wywoływane (*callees*) oraz komentarze w zmodyfikowanych plikach — czy zmiany są zgodne z gwarancjami opisanymi w komentarzach/docstringach.

#### Angle D — Language-pitfall specialist *(wymagany w `high` / `xhigh` / `max` oraz przy widocznych pułapkach językowych)*
Przeskanuj diff pod kątem klasycznych pułapek danego języka i frameworka:
- **Python:** domyślne argumenty mutowalne (`def f(x=[])`), *late-binding closures* w pętlach/comprehension, modyfikacja słownika/listy podczas iteracji, brak zamknięcia zasobów/uchwytów, porównania `== None` zamiast `is None`, mutacja współdzielonych atrybutów klasy.
- **JS/TS:** *falsy-zero*, koercja `==`, zmienna pętli przechwycona w domknięciu, nieobsłużone odrzucenie Promise.
- **Go / Rust / C++:** zapis do `nil` mapy, przechwycenie zmiennej pętli, wyścigi danych, niebezpieczne rzutowania / UB.
- **Ogólne:** SQL / command / path injection, dryf stref czasowych (naive vs. aware datetime / DST), porównywanie liczb zmiennoprzecinkowych przez `==`.

#### Angle E — Wrapper/proxy correctness *(gdy diff dodaje lub modyfikuje wrapper, dekorator, cache, proxy lub adapter)*
Sprawdź, czy każda metoda wrappera deleguje wywołanie do opakowanej instancji (`delegate`), a nie z powrotem przez globalny rejestr lub sesję (co prowadziłoby do ponownego wejścia w cache lub nieskończonej rekurencji). Sprawdź też, czy wrapper przekazuje wszystkie metody i właściwości, których wywołujący faktycznie używają.

---

### Kąty jakości, architektury i konwencji (*Cleanup, Altitude & Conventions Angles*)

*(Błędy poprawności z Angles A–E mają zawsze wyższy priorytet niż poniższe kąty, jeśli trzeba ograniczyć liczbę wyników do limitu).*

#### Reuse
Wskaż nowy kod, który reimplementuje funkcjonalność już obecną w repozytorium — przeszukaj (`grep_search`) moduły współdzielone/narzędziowe oraz sąsiednie pliki i podaj nazwę istniejącego helpera, którego należy użyć.

#### Simplification
Wskaż zbędną złożoność dodaną przez diff: nadmiarowy lub wyliczalny stan, copy-paste z niewielką wariacją, głębokie zagnieżdżenia, pozostawiony martwy kod. Wskaż prostszą postać wykonującą to samo zadanie.

#### Efficiency
Wskaż marnowaną pracę wprowadzoną przez diff: powtarzane obliczenia lub wielokrotne I/O, niezależne operacje wykonywane sekwencyjnie, operacje blokujące dodane do ścieżki startowej lub *hot path*. Zgłaszaj też długo żyjące obiekty zbudowane z domknięć (*closures*), które utrzymują przy życiu cały otaczający scope z dużymi strukturami (wyciek pamięci) — wskaż tańszą alternatywę (np. klasę/strukturę kopiującą tylko potrzebne pola).

#### Altitude
Sprawdź, czy każda zmiana naprawia przyczynę źródłową (*root cause*) na odpowiedniej głębokości architektury, zamiast łatać symptom kruchym obejściem (*bandaid*). Doklejanie przypadków szczególnych na wspólnej infrastrukturze to znak, że naprawa jest zbyt płytka — wskaż prostszą, bardziej ogólną zmianę w mechanizmie bazowym.

#### Conventions (`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`)
Sprawdź diff pod kątem wyraźnych naruszeń reguł zapisanych w plikach reguł projektu. Zgłoś naruszenie **wyłącznie wtedy**, gdy potrafisz zacytować dokładną regułę i dokładną linię kodu, która ją łamie — żadnych subiektywnych preferencji stylowych ani domysłów.

---

## Phase 2 — Weryfikacja i odsiew (`CONFIRMED` / `PLAUSIBLE` / `REFUTED`)

1. **Deduplikacja:** Połącz kandydatów wskazujących na tę samą linię lub ten sam mechanizm błędu, zachowując wpis z najbardziej konkretnym scenariuszem awarii.
2. **Weryfikacja każdego kandydata** (przez subagenta weryfikującego w trybie *Multi-Agent* lub przez rygorystyczne sprawdzenie kodu źródłowego w trybie *Inline*). Przypisz każdemu kandydatowi dokładnie jeden status (oraz ocenę pewności `0–100`):
   - **`CONFIRMED` (90–100 pkt):** Potrafisz podać konkretne dane wejściowe/stan wyzwalający problem oraz błędny wynik lub crash. Zacytuj dokładną linię kodu.
   - **`PLAUSIBLE` (75–89 pkt):** Mechanizm błędu jest realny, a wyzwalacz zależy od realistycznego stanu runtime: wyścigi współbieżności, `None`/`undefined` na rzadkiej, ale osiągalnej ścieżce (obsługa błędów, zimny cache, brakujące pole opcjonalne), *falsy-zero*, *off-by-one* na brzegu niechronionym przez kod, utracony anchor w regexie/allowliście. Napisz, co potwierdziłoby błąd.
   - **`REFUTED` (< 75 pkt — ODRZUĆ):** Twierdzenie jest faktycznie nieprawdziwe (kod robi co innego), matematycznie/typowo niemożliwe (udowodnione przez typ, stałą lub niezmiennik), już zabezpieczone guardem w tym diffie lub otaczającym kodzie, dotyczy linii niezmodyfikowanych i niezwiązanych z diffem (*pre-existing issue*) albo jest czysto stylistycznym nitpickiem bez obserwowalnego wpływu.
3. **Filtrowanie:**
   - Odrzuć wszystkie znaleziska ze statusem **`REFUTED`**.
   - Zachowaj **`CONFIRMED`** oraz **`PLAUSIBLE`**, sortując je od najwyższej wagi (najpierw błędy poprawności `correctness`, potem `efficiency` / `altitude` / `reuse` / `simplification` / `conventions`).

---

## Phase 3 — Sweep for gaps *(tylko dla poziomów `high`, `xhigh`, `max`)*

Mając zweryfikowaną listę znalezisk, wykonaj jeszcze jeden przebieg po diffie i otaczających funkcjach jako świeży recenzent, szukając **wyłącznie** defektów, których nie ma jeszcze na liście. Skup się na tym, co pierwszy przebieg najczęściej pomija:
- przeniesiony lub wyekstrahowany kod, który zgubił guarda lub anchora,
- pułapki drugiego rzędu (wartość domyślna w `dataclass` ewaluowana raz, niedeterminizm `hash()`, zawężenie zakresu locka, metody predykatowe ze skutkami ubocznymi),
- asymetria `setup` / `teardown` w testach,
- przypadkowo odwrócone wartości domyślne konfiguracji lub flag.

---

## Format raportu końcowego

Przedstaw wynik w przejrzystej strukturze po polsku:

```markdown
### Code Review — Podsumowanie
- **Zakres:** `<opis badanego diffu / PR / plików>`
- **Tryb:** `<effort: low|medium|high|max>` (`<Single-Pass Inline | Multi-Agent Fan-Out>`)
- **Wynik:** Znaleziono **X** problemów (`Y CONFIRMED`, `Z PLAUSIBLE`)

---

### Znaleziska

#### 1. `[CONFIRMED | 95/100]` `[<kategoria>]` <Jednozdaniowe podsumowanie problemu>
- **Lokalizacja:** [plik.py:L42-L48](file:///pelna/sciezka/plik.py#L42-L48)
- **Failure scenario / Koszt:** <Konkretne dane wejściowe lub stan → błędny wynik/crash (lub cytat złamanej reguły z GEMINI.md/CLAUDE.md)>
- **Proponowana poprawka:**
  ```<język>
  <konkretny kod naprawiający problem>
  ```
```

Jeśli po weryfikacji nie znaleziono żadnych problemów spełniających kryteria, napisz krótko:
> **Brak problemów (`0 CONFIRMED / PLAUSIBLE`).** Sprawdzono diff pod kątem poprawności (Angles A–E), jakości (Reuse, Simplification, Efficiency, Altitude) oraz zgodności z regułami projektu.

---

## Akcje po przeglądzie (`--fix` oraz `--comment`)

1. **Gdy przekazano flagę `--fix`:**
   - Po wypisaniu raportu od razu zaaplikuj poprawki w drzewie roboczym używając narzędzi edycji plików (`replace_file_content`).
   - Pomiń poprawki, które zmieniłyby zamierzone zachowanie biznesowe lub wymagałyby głębokiej przebudowy poza zakresem diffu (odnotuj powód pominięcia).
   - Na końcu wypisz krótkie podsumowanie: co zostało naprawione, a co pominięto.
2. **Gdy przekazano flagę `--comment`:**
   - **Dla GitHub PR:** opublikuj zwięzły komentarz podsumowujący przez `gh pr comment <pr> --body "..."` lub komentarze inline przez `gh api repos/{owner}/{repo}/pulls/{pr}/comments`. W linkach do kodu na GitHubie używaj pełnego 40-znakowego SHA1 commita (`https://github.com/owner/repo/blob/<pelne-sha1>/sciezka/plik.ext#L10-L15`). **Pamiętaj o globalnej regule użytkownika: NIGDY nie dodawaj stopki AI attribution (`Generated with...`, `Co-authored-by:` itp.).**
   - **Dla GitLab MR:** opublikuj notatkę przez `glab mr note <mr> -m "..."`.
