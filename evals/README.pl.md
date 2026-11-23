# Benchmark recenzji z zasianymi defektami

Tani i powtarzalny sposób na zmierzenie, ile rzeczywistych defektów wykrywa protokół recenzji na dowolnej platformie agentowej: Craftsman `/craft-review` zestawiony ze zwykłym promptem „zrecenzuj tę zmianę”, w Claude Code, Google Antigravity, Copilocie, Codexie lub każdym innym narzędziu potrafiącym czytać repozytorium Git.

Każdy **przypadek (case)** to niewielka biblioteka lub CLI w Pythonie (wyłącznie stdlib, z własnymi testami `unittest`) oraz pull request realizujący zadane wymaganie. Pull request zawiera kilka **zasianych defektów**, które wyglądają na uczciwe pomyłki, a dostarczone testy przechodzą na zielono — tak jak w prawdziwym PR-ze z zielonym CI. Recenzja jest oceniana na podstawie tego, ile zasianych defektów wskazują jej znaleziska.

Zestaw liczy 8 przypadków i 27 defektów: 6 małych przypadków (po 2 lub 3 defekty) oraz 2 duże, które pokazują, jak koszt skaluje się wraz z rozmiarem zmiany. Przed wyciąganiem wniosków uruchom dany przypadek kilkukrotnie (3 lub więcej razy): wyniki modelu różnią się między uruchomieniami. Aby automatycznie zmierzyć skuteczność wykrywania (recall), koszt i czas rzeczywisty prawdziwej recenzji w Claude Code, zobacz [Pomiar recenzji od końca do końca](#pomiar-recenzji-od-końca-do-końca-agent-compare).

| Przypadek | Co robi PR | Rodzaje defektów |
| :--- | :--- | :--- |
| `invtool` | polecenie `search` dla CLI do inwentaryzacji w CSV | `qualifier-not-implemented`, `help-contradicts-code` |
| `kvstore` | atomowe zapisy, migawki i przestrzenie nazw dla magazynu klucz-wartość w JSON | `non-atomic-write`, `aliasing` |
| `quotes` | wyceny w innych walutach, z pamięcią podręczną (cache) | `cache-key-missing-param`, `swallowed-error`, `aliasing` |
| `attach` | foldery w załącznikach zgłoszeń, eksport do zip | `path-traversal`, `platform-dependence` |
| `reminders` | dzienny skrót i czasy lokalne dla przypomnień | `platform-dependence`, `off-by-one-boundary`, `weak-test` |
| `taskboard` | priorytety zadań w modelu, JSON, CLI i CSV | `incomplete-propagation`, `empty-as-missing`, `weak-test` |
| `orderdesk` (duży) | wysyłka dla obsługi zamówień: strefy, tabele stawek przewoźników, próg darmowej dostawy, numery śledzenia i zdarzenia przewoźnika (~670 zmienionych linii, 14 plików) | `cache-key-missing-param`, `off-by-one-boundary`, `empty-as-missing`, `incomplete-propagation` (między plikami), `swallowed-error`, `weak-test` |
| `feedbox` (duży) | tagi kanałów, eksport do plików Markdown/HTML wraz z indeksem oraz skrót dla czytnika RSS (~450 zmienionych linii, 15 plików) | `path-traversal`, `platform-dependence`, `aliasing`, `help-contradicts-code`, `qualifier-not-implemented`, `incomplete-propagation` (między plikami) |

Każdy rodzaj defektu występuje co najmniej raz: `off-by-one-boundary`, `empty-as-missing`, `swallowed-error`, `non-atomic-write`, `incomplete-propagation`, `weak-test` (test, który nie może nie przejść, albo osłabiona asercja), `aliasing` (płytka kopia lub mutowalna wartość domyślna), `cache-key-missing-param`, `platform-dependence` (separator ścieżki, naiwne vs. świadome strefy czasowej `datetime`), `path-traversal`, `help-contradicts-code` oraz `qualifier-not-implemented`. Każdy mały przypadek zawiera także 1 lub 2 **pułapki (decoys)** (duże po 3): fragmenty kodu wyglądające podejrzanie, ale poprawne. Zgłoszenie dotyczące pułapki jest raportowane jako trafienie w pułapkę (*decoy hit*). W zestawie nie ma górnego limitu liczby defektów.

## Pomiar recenzji od końca do końca (`agent`, `compare`)

`python3 -m evals.run agent` uruchamia dla każdego przypadku realną, bezobsługową sesję Claude Code (`claude -p`), ocenia jej znaleziska poniższym mechanizmem punktującym i dopisuje jeden wiersz JSON na uruchomienie do pliku wynikowego. Mierzy to, na podstawie czego należy oceniać zmianę protokołu: **recall, koszt i czas rzeczywisty**.

Automatyczny harness działa wyłącznie na Linuksie i macOS: uruchamia sesję przez wrapper powłoki POSIX, a kilka przypadków ma zasiane defekty ujawniające się tylko na ścieżkach POSIX.

```bash
# jedno uruchomienie każdego przypadku z bieżącego checkoutu (ok. 8 uruchomień, zob. "Ile to kosztuje")
python3 -m evals.run agent --cases all --arm current --out bench/evals/current.jsonl

python3 -m evals.run agent --help
python3 -m evals.run compare bench/evals/current.jsonl
python3 -m evals.run breakdown --out bench/evals/current.jsonl   # na co poszły subagenty i tokeny
```

| Flaga | Znaczenie |
| :--- | :--- |
| `--cases <ids\|all>` | lista identyfikatorów przypadków rozdzielona przecinkami, domyślnie `all` |
| `--craftsman-src <dir>` | badany checkout Craftsmanna, domyślnie to repozytorium |
| `--arm <label>` | nazwa konfiguracji w wynikach (`main`, `compact-prompt`, `baseline`) |
| `--model <name>` | domyślnie `sonnet`; zachowaj taki sam między ramionami porównania |
| `--repeat N` | liczba uruchomień na przypadek, domyślnie 1; kolejne wywołanie kontynuuje numerację po wcześniejszych |
| `--jobs N` | liczba równoległych uruchomień, domyślnie 2 |
| `--budget-usd N` | limit wydatków pojedynczego uruchomienia (`--max-budget-usd`), domyślnie 5 |
| `--timeout-min N` | limit czasu zegarowego pojedynczego uruchomienia, domyślnie 30 |
| `--out <file>` | plik wynikowy, do którego dopisywane są rekordy; domyślnie `bench/evals/<arm>.jsonl`. Logi trafiają do `<rdzeń nazwy pliku>.logs/` |
| `--prompt <template or file>` | prompt; `{skill}`, `{base}`, `{task_file}` i `{case}` są podstawiane automatycznie |
| `--baseline` | wariant całkowicie bez Craftsmanna: bez wtyczki, bez polecenia `craftsman`, z poniższym promptem bazowym |
| `--keep-workdirs`, `--dry-run` | zachowuje tymczasowe repozytoria; wypisuje pierwsze polecenie i kończy działanie |
| `--claude`, `--permission-mode`, `--allow-tool` | plik wykonywalny, tryb uprawnień, dodatkowa reguła dozwolonego narzędzia |

**Co robi jedno uruchomienie.** Przygotowuje przypadek w świeżym katalogu tymczasowym (`repo/`, ze stanem bazowym zatwierdzonym w commicie oraz niezacommitowaną zmianą, dokładnie tak jak `prepare`), ustawia skrypt wrappera `craftsman` na początku `PATH` (`exec python3 <src>/bin/craftsman-cli.py "$@"`, aby sesja uruchamiała CLI z badanego checkoutu, a nie z wersji zainstalowanej w systemie ani katalogu `craftsman/` w recenzowanym repozytorium), uruchamia `claude` w `repo/`, ocenia rejestr w `repo/.git/craftsman/ledger.json` i dopisuje rekord. Badany checkout jest kopiowany raz na wywołanie, bez `.git`, `evals/`, `docs/` i `tests/`: badane ramię nie może się zmienić w trakcie długiego przebiegu, a sesja nie może znaleźć pliku `truth.json` obok wtyczki. Rekord zapisuje źródłowy commit (`+dirty`, gdy śledzone pliki wtyczki miały niezacommitowane zmiany).

**Jak ładowany jest Craftsman.** Poprzez `--plugin-dir <kopia src>`. Manifest wtyczki (`.claude-plugin/plugin.json`) dostarcza skille (jako `craftsman:<skill>`), agentów `craftsman-reviewer`, `craftsman-verifier` oraz `craftsman` (jako `craftsman:<agent>`) i hooki cyklu życia, które zapisują w rejestrze dowody izolacji. Harness odczytuje zdarzenie `system`/`init` ze strumienia i **natychmiast zatrzymuje uruchomienie** (status `load-failed`, niemal zerowy koszt), jeśli brakuje w nim skilla recenzji lub któregoś z dwóch agentów — a w przypadku `--baseline`, jeśli załadował się jakikolwiek skill lub agent `craftsman:`. Rekord zapisuje, jak często uruchamiał się każdy hook i ile razy zawiódł. Skillem recenzji jest `skills/craft-review`, jeśli źródła go zawierają, w przeciwnym razie (checkouty starsze niż Craftsman 5) `skills/code-review`; domyślny prompt to:

```text
/craftsman:craft-review review the changes against HEAD (review only, do not fix). The change description is in .git/evals/task.md.
```

**Uprawnienia.** Praca bezobsługowa z jawną listą dozwolonych narzędzi i bez `bypassPermissions`: `--permission-mode dontAsk` (wszystko spoza listy jest odrzucane zamiast pytania użytkownika, więc nic nie zawiśnie na monicie) plus `--allowedTools`:

- `Skill`, `Task`/`Agent` (subagenty), `TodoWrite`;
- `Bash(craftsman:*)`, polecenia Gita tylko do odczytu (`status`, `diff`, `log`, `show`, `grep`, `ls-files`, `ls-tree`, `cat-file`, `rev-parse`, `rev-list`, `merge-base`, `blame`, `branch`), `python3`/`python` (do uruchamiania testów i reprodukowania znalezisk), `cd`, `pwd`, `ls`, `wc`;
- `Read`, `Grep` i `Glob` nie wymagają dodatkowych reguł: Claude Code pozwala na nie wewnątrz katalogu roboczego, czyli tymczasowego przypadku, a `dontAsk` blokuje je poza nim (sprawdzone: `Read /etc/hostname` zostaje odrzucone);
- `--disallowedTools Edit,Write,NotebookEdit,WebFetch,WebSearch`: recenzja nie edytuje plików ani niczego nie wyszukuje w sieci (w ramieniu baseline `Write` jest dozwolone wyłącznie dla `.git/evals/findings.json`).

`git stash`, `checkout`, `reset`, `rm` i tym podobne nie znajdują się na liście, czego wymaga również sam protokół recenzji. Dlaczego nie `bypassPermissions`: tryb ten pozwoliłby źle zachowującemu się przebiegowi zmienić albo skasować dowolny plik dostępny dla użytkownika, a pomiar powinien głośno zgłosić naruszenie (odrzucone wywołanie trafia do `permission_denials` w rekordzie), zamiast udać sukces kosztem złamania protokołu. Czego to nie załatwia: reguły Basha są regułami prefiksowymi, a nie pełną piaskownicą; `python3` może odczytać dowolny plik, a sesja dziedziczy sieć i poświadczenia użytkownika. Katalogiem roboczym każdej sesji jest jej własny katalog tymczasowy i tylko tam harness zapisuje dane, lecz jeśli bezpieczeństwo maszyny ma znaczenie, uruchamiaj całość w kontenerze lub jednorazowej maszynie wirtualnej. Sesja otrzymuje również tylko ustawienia projektu i lokalne (`--setting-sources project,local`) oraz brak serwerów MCP (`--strict-mcp-config`), aby wtyczki użytkownika, skille, pamięć i konektory nie różniły się między maszynami czy ramionami.

**Rekord wyniku** (jeden obiekt JSON w linii): `arm`, `case`, `run`, `status` (`ok`, `budget`, `timeout`, `max-turns`, `error`, `load-failed`, `no-result`, `harness-error`), `recall` (znalezione, łącznie, ID znalezione i pominięte, trafienia w pułapki, niedopasowane znaleziska, duplikaty, zignorowane odrzucone znaleziska), `cost_usd`, `duration_ms` (własny zegar Claude'a: zsumowany czas każdej tury głównej pętli), `wall_s` (zegar harnessa), `turns` (zsumowane po każdej turze), `results` (liczba tur: sesja wypisuje zdarzenie `result` dla każdej tury głównej pętli, a każdy raport subagenta w tle otwiera kolejną, więc recenzja wypisuje ich wiele), `subagents` (uruchomieni, ukończeni, nieudani), `model_usage` (tokeny i koszt w podziale na modele, skumulowane dla całej sesji wraz z subagentami) i `usage` (tokeny wyłącznie z ostatniej tury głównej pętli), `tool_calls`, `hooks`, `permission_denials` (wywołania narzędzi odrzucone w całej sesji przez tryb uprawnień lub hooka), `hook_errors` (`count` wpisów w `<git-dir>/craftsman/hook-errors.log` danego checkoutu oraz `first`: hook, który uległ awarii, nadal odpowiada, więc `hooks` pokazuje go jako sukces i dopiero ten log ujawnia błąd; `compare` pokazuje sumę w kolumnie `note`, np. `13 hook errors`), sprawdzenie `init`, prompt, commit źródłowy oraz `logs`: strumień (`stream.jsonl` bez linii zdarzeń strumieniowych na poziomie tokenów), `stderr.txt`, rejestr uruchomienia oraz kopia logu błędów hooków (`hook_errors`, jeśli istnieje). Rekordy zapisane przed wprowadzeniem sumowania tur lub odczytu logu błędów hooków zawierają wartości z ostatniej tury i nie mają `hook_errors`; `rescore` ponownie wczytuje oba elementy ze strumienia oraz zachowanej kopii logu (albo z zachowanego checkoutu przypadku). `status` inny niż `ok` powoduje zakończenie polecenia z kodem wyjścia 1. Przebiegi ze statusem `load-failed`, `no-result`, `error` lub `harness-error` są wykluczane ze wszystkich średnich w `compare` i zliczane w uwadze `invalid`; przebiegi `budget` i `timeout` są zachowywane wraz ze znaleziskami zapisanymi przed przerwaniem, ponieważ protokół przekraczający budżet również jest wynikiem.

### Porównanie dwóch protokołów (A/B)

```bash
git worktree add ../craftsman-a main            # protokół obecny
git worktree add ../craftsman-b my-experiment   # protokół zmodyfikowany

# przeplataj ramiona, aby dryf szybkości API działał na oba tak samo
for i in 1 2 3 4 5; do
  python3 -m evals.run agent --craftsman-src ../craftsman-a --arm a --out bench/evals/ab.jsonl --jobs 2
  python3 -m evals.run agent --craftsman-src ../craftsman-b --arm b --out bench/evals/ab.jsonl --jobs 2
done

python3 -m evals.run compare bench/evals/ab.jsonl --defects
```

Wszystko poza samym checkoutem powinno pozostać równe: model, `--jobs` (równoległe uruchomienia współdzielą API i maszynę, więc czasy rzeczywiste są porównywalne wyłącznie przy tym samym `--jobs`; użyj `--jobs 1`, gdy pytaniem jest czas), prompt i budżet. Oba ramiona mogą dzielić jeden plik wynikowy (rozróżnia je etykieta `arm`), a `agent` można uruchomić później ponownie, aby dopisać powtórzenia: numery uruchomień dla danego ramienia i przypadku kontynuują numerację po tych już obecnych w pliku (lub w jego folderze logów), więc wcześniejsze przebiegi, logi i zachowane rejestry nigdy nie są nadpisywane. Ramię bazowe do takiego samego porównania: `--baseline --arm baseline`.

Zmiany zasad punktacji (nowa reguła dopasowania, skorygowana lokalizacja w prawdzie referencyjnej) nie wymagają nowych uruchomień: `agent` zachowuje rejestr każdego uruchomienia, a `python3 -m evals.run rescore bench/evals/ab.jsonl` ponownie ocenia te rejestry względem aktualnej prawdy i przepisuje `recall` każdego rekordu w miejscu. Oryginalny plik zostaje zachowany jako `ab.jsonl.bak` (`.bak.1`, ..., gdy ten już istnieje). Polecenie ponownie odczytuje także `turns`, `results`, `duration_ms`, `permission_denials` oraz `hook_errors` z zachowanych logów (zob. opis rekordu wyniku powyżej). Rekordy bez zachowanego rejestru (przebiegi baseline, przebiegi bez znalezisk) utrzymują swój dotychczasowy recall, z ostrzeżeniem.

`compare` wypisuje dla każdego ramienia i przypadku: liczbę uruchomień, recall (średnia wykrytych defektów / zasianych), najmniejszą i największą liczbę wykryć w jednym przebiegu, trafione pułapki, niedopasowane znaleziska (możliwe fałszywe alarmy), średni koszt w USD wraz z rozrzutem między powtórzeniami, minuty, tury, uruchomionych subagentów, **tokeny** (cała sesja wraz z subagentami: input niecache'owany, output, odczyt z cache i zapis do cache) oraz **$/defect**, czyli koszt jednego znalezionego defektu (średni koszt podzielony przez średnią liczbę znalezionych defektów zasianych; `-`, gdy nie znaleziono żadnego); na końcu drukuje wiersz TOTAL (jedno pełne przejście po wszystkich przypadkach; jego zakres to recall całego zestawu w poszczególnych powtórzeniach, a `$/defect` to koszt przejścia przez znalezione w nim defekty). Gdy ramion jest kilka, dopisuje dla każdego kolejnego ramienia ilorazy kosztu, minut, tur, tokenów i kosztu na wykryty defekt w każdym przypadku oraz w wierszu TOTAL, a także **różnicę recallu całego zestawu z 90% przedziałem** (test t Welcha na wynikach recallu zestawu per powtórzenie). `--tokens` dodaje rozbicie tokenów (input, output, cache read, cache write). `--defects` pokazuje, jak często znajdowano każdy pojedynczy zasiany defekt — to liczba, na którą warto patrzeć, gdy sumy wyglądają identycznie: spadek wykrywalności konkretnego defektu z 5/5 do 1/5 to regresja, którą średnia potrafi ukryć. `--json` wypisuje te same tabele jako JSON, ze wszystkimi kolumnami, średnimi tokenów i liczbowym kosztem na wykryty defekt oraz, dla każdego kolejnego ramienia, jego porównaniem z pierwszym (`versus`).

Tokeny sesji są odczytywane z pola `model_usage` rekordu, zsumowane po wszystkich modelach: stąd też liczony jest koszt, dzięki czemu obejmuje każdego agenta sesji. Pole `usage` nie jest używane: dotyczy wyłącznie ostatniej tury pętli głównej (0.3–7.6% całej sesji w przebiegach z subagentami), więc zaniżałoby zużycie sesji. Rekord bez zliczeń tokenów w `model_usage` jest odnotowywany w kolumnie `note` (`1 without tokens`) i wykluczany ze średnich tokenowych.

### Gdzie trafiają tokeny (`breakdown`)

```bash
python3 -m evals.run breakdown --out bench/evals/ab.jsonl              # per ramię, wszystkie przypadki łącznie
python3 -m evals.run breakdown --out bench/evals/ab.jsonl --cases      # oraz wiersz dla każdego przypadku
python3 -m evals.run breakdown --out bench/evals/ab.jsonl --arm b --json
```

Recenzje osiągają dobre wyniki, ale dużo kosztują, a `compare` pokazuje jedynie sumę. `breakdown` łączy każdy rekord z zachowanym przez uruchomienie rejestrem (`logs.ledger`) i wypisuje dla każdego ramienia (oraz per przypadek z flagą `--cases`) zestaw następujących tabel:

| Tabela | Co zawiera |
| :--- | :--- |
| Runs | prawidłowe przebiegi, ile z nich zachowało rejestr i pełne zliczenia tokenów, średni koszt, średnie minuty, koszt na znaleziony defekt |
| Subagents | na uruchomienie z rejestrem: łowcy i weryfikatorzy (wznowiony agent liczony jest raz); `relaunched slots`: sloty polowania uruchamiane więcej niż raz (łowca zakończył pracę lub został wznowiony); `idle verifiers`: uruchomienia weryfikatorów, które nie potwierdziły żadnego werdyktu ani scalenia (odrzucono im polecenia, przerwały pracę lub się poddały, więc ich znaleziska przeszły do innego weryfikatora; rejestr nie zapisuje, które znaleziska kazano ocenić danemu weryfikatorowi, więc ponowne uruchomienia weryfikatorów nie są liczone jako takie, a weryfikator, którego transkryptu nie dało się odczytać, jest przypisywany po czasie i może dostać zasługę za werdykt sąsiada, co czyni tę wartość dolnym ograniczeniem); `second opinions`: uruchomienia weryfikatorów, które oceniły wyłącznie znaleziska ocenione już wcześniej przez innego weryfikatora, przy czym scalenie liczy się jako ocena tak samo jak na bramce (poważny błąd odrzucony jako duplikat wymaga drugiego weryfikatora) |
| Hunters | na uruchomienie: w podziale na rodzaj slotu, na który polowali (`bug`, `simplicity`, `sweep`, `variant`, `fresh` albo `none`, gdy żaden slot ich nie tłumaczy) oraz na jego generację (`gen 1`; kolejne fale to `gen 2` i `gen 3`) |
| Tokens | na uruchomienie z pełnymi zliczeniami tokenów: input, output, cache read, cache write, suma oraz minuty pracy agentów dla łowców i dla weryfikatorów (ze zdarzeń stop w rejestrze, tak jak zlicza je `craftsman review report`), dla **sesji głównej** (cała sesja minus jej subagenty) i dla całej sesji, wraz z udziałem procentowym każdego komponentu |
| Cache write per subagent | średnia i minimum, dla łowców oraz dla weryfikatorów |
| Findings | na uruchomienie: zgłoszone, scalone z innym oraz według bieżącego werdyktu (`proven`, `probable`, `question`, `dismissed`, `open`) |
| Seeded defects found | sumy: dla każdego znalezionego zasianego defektu — slot, który zapisał go jako pierwszy, według rodzaju slotu (`lead` to znalezisko samego orkiestratora) i generacji |
| Findings on no seeded defect | sumy: znaleziska, których scorer nie dopasował do żadnego zasianego defektu albo dopasował do pułapki (łącznie z odrzuconymi, bez scalonych duplikatów), według rodzaju slotu — szum, który musieli ocenić weryfikatorzy |

Raport kończy się jedną linią na ramię: `cost per found defect`, oraz wymienia z nazwy każdy przebieg pominięty przy danej liczbie, podając powód. Nic nie jest szacowane: przebieg bez zachowanego rejestru (przebieg baseline, sesja, która nigdy nie przygotowała recenzji) jest wyłączany ze wszystkich statystyk rejestru, a przebieg, w którym subagent nie ma zapisanego zużycia w rejestrze (nie udało się odczytać jego transkryptu), którego rekord nie ma zliczeń tokenów albo którego subagenty raportują więcej tokenów danego typu niż cały rekord (oba źródła się nie zgadzają, więc udział sesji głównej byłby ujemny), jest wyłączany wyłącznie ze statystyk tokenowych.
Nieprawidłowe przebiegi są wykluczane ze wszystkiego, tak jak w `compare`.

Wszystko spajają te same reguły, których używają sam produkt i scorer, bez żadnych nowych zasad: łowca należy do slotu, z którym wiąże go `craftsman.attribution` (jego deklaracja widoczna w transkrypcie), albo do slotu obsługiwanego przez uruchomienie bez czytelnego transkryptu; werdykt przypada uruchomieniom weryfikatorów, które za nim stoją; znalezisko przypada zapisanemu slotowi powiązanemu z uruchomieniem, które je zapisało, albo slotowi, który wymienia; znalezisko łączy się z zasianym defektem zgodnie z regułą dopasowania `score`, a „pierwsze” oznacza znalezisko zapisane najwcześniej. `breakdown` wyłącznie czyta: nie uruchamia żadnego agenta i nie zmienia ani pliku wyników, ani rejestru.

**Jak to czytać.**

- *Czy kolejne fale są warte swojego kosztu?* Zestaw kolumny `gen 2` i `gen 3` w tabeli *Hunters* (co uruchamiają fale) z tymi samymi kolumnami w *Seeded defects found* (defekty wykryte jako pierwsze dopiero przez falę) oraz kolumnami `variant` i `fresh` w *Findings on no seeded defect* (szum, jaki fale dokładają weryfikatorom). Fale uruchamiające wielu łowców, które znajdują mało nowych defektów jako pierwsze i produkują szum, warto ciąć w pierwszej kolejności; potwierdź za pomocą `compare`, że recall nie spadł.
- *Ile wynosi stały kontekst startowy?* Minimalne `cache write per subagent` odpowiada w przybliżeniu temu, ile każdy subagent płaci jeszcze zanim cokolwiek przeczyta (prompt systemowy, definicja agenta, narzędzia, brief); pomnożone przez liczbę subagentów w przebiegu wyznacza dolny pułap zapisów do cache jednego uruchomienia. Średnia znacznie wyższa od minimum oznacza, że dominuje właściwa praca, a nie start; średnia bliska minimum podpowiada, że oszczędności tokenów trzeba szukać w krótszym kontekście startowym albo mniejszej liczbie subagentów.
- *Ile kosztuje sesja główna?* Pokazuje to udział wiersza *main session*. Własna telemetria produktu (`review report`) zlicza wyłącznie subagentów, więc tutaj jako jedyne miejsce widać tokeny samego orkiestratora. Duży udział wskazuje na koszt kroków samego protokołu (czytanie briefów, sprawdzenia, raporty), a nie samego polowania.

### Ile to kosztuje i ilu powtórzeń wymaga wniosek

*Koszt.* Uruchomienia = ramiona x przypadki x powtórzenia; wydatek jednego uruchomienia ogranicza flaga `--budget-usd`, więc górny pułap to uruchomienia x limit (8 przypadków x 5 powtórzeń x 2 ramiona = 80 uruchomień, 400 USD przy domyślnym limicie 5). Sesja, która tylko ładuje wtyczkę i wykonuje jedno polecenie, kosztuje około 0.06–0.15 USD (zmierzone na `sonnet`); prawdziwa recenzja kosztuje znacznie więcej, a duże przypadki są droższe od małych — po to właśnie powstały. Zmierz przed planowaniem: uruchom po jednym powtórzeniu `kvstore` oraz `orderdesk`, odczytaj kolumnę kosztu i ustaw `--budget-usd` mniej więcej dwukrotnie wyżej niż najdroższy przebieg. `compare` wypisuje koszt jednego pełnego przejścia po wszystkich przypadkach.

*Powtórzenia.* Pojedyncze uruchomienie to tylko anegdota: ten sam prompt na tym samym przypadku znajduje różną liczbę defektów między uruchomieniami. Porównywalną jednostką jest **recall całego zestawu w jednym powtórzeniu** (wszystkie przypadki uruchomione po razie). Jeśli waha się ono z odchyleniem standardowym sigma między powtórzeniami, to 90% przedział różnicy dwóch ramion po n powtórzeń każde wynosi w przybliżeniu +-t x sigma x sqrt(2/n) (t wynosi 2.1 przy n=3, 1.9 przy n=5, 1.7 przy n=10), stąd:

| Powtórzeń na ramię | Najmniejszy spadek recallu, jaki przedział potrafi wykluczyć (sigma = 0.10) |
| :---: | :---: |
| 3 | około 17 punktów |
| 5 | około 12 punktów |
| 10 | około 8 punktów |
| 20 | około 5 punktów |

Równoważnie, aby wykryć rzeczywistą różnicę d z mocą 80% przy poziomie istotności 5% (jednostronnie), potrzeba około `12.4 x sigma^2 / d^2` powtórzeń na ramię: przy sigma = 0.10 dla d = 0.20 potrzeba 4, dla d = 0.15 potrzeba 6, a dla d = 0.10 potrzeba 13. Wartość sigma = 0.10 jest założeniem wstępnym: uruchom najpierw 5 powtórzeń obecnego protokołu, odczytaj rozrzut recallu zestawu w wierszu TOTAL polecenia `compare` (oraz `±` dla pojedynczych przypadków) i zastąp ją zmierzoną wartością. Praktyczna zasada: 3 powtórzenia wykrywają tylko duże różnice (20 punktów); aby twierdzić, że przy optymalizacji tokenów albo czasu „recall nie spadł”, użyj co najmniej 5 powtórzeń na ramię i podaj margines, jaki przedział faktycznie wyklucza — albo 10, jeśli margines ma zejść poniżej 10 punktów. Dokładaj duże przypadki zamiast samych powtórzeń, gdy przedział jest szeroki z powodu kilku trudnych defektów: `--defects` pokaże których.

## Ręczne uruchomienie przypadku

```bash
python3 -m evals.run list                               # przypadek, rozmiar, defekty, pułapki, rodzaje
python3 -m evals.run prepare quotes ~/evals/quotes-1     # świeży, pusty katalog
```

To tryb dla innych agentów i platform albo do obejrzenia konkretnego przypadku. Polecenie `agent` powyżej robi dokładnie to samo bezobsługowo dla Claude Code.

`prepare` tworzy repozytorium Git w katalogu docelowym, zatwierdza drzewo `base/` danego przypadku, a następnie kopiuje na nie `head/` **bez zatwierdzania commita**, więc pull request jest niezacommitowanym diffem (`git diff HEAD` plus nowe pliki). Opis zmiany (`task.md`) kopiowany jest do `.git/evals/task.md`, poza drzewem roboczym. `truth.json` nigdy nie jest kopiowany. Dla każdego uruchomienia użyj nowego katalogu docelowego; recenzent nie może edytować plików (numery linii w `truth.json` odnoszą się do `head/`).

### Z Craftsmanem

Zainstaluj Craftsmanna raz dla danej platformy (`craftsman install`, zob. [instalacja](../docs/installation.md)), uruchom agenta w przygotowanym katalogu i wpisz:

```text
/craft-review review the changes against HEAD (review only, do not fix)
```

Wskaż mu, że opis zmiany znajduje się w pliku `.git/evals/task.md` (można go przekazać jako zadanie recenzji albo kazać agentowi go przeczytać). Znaleziska trafiają do `.git/craftsman/ledger.json`, skąd odczytuje je `score`. Znaleziska odrzucone przez recenzję są ignorowane.

### Bez Craftsmanna (baseline)

Uruchom agenta w przygotowanym katalogu bez żadnych skilli, reguł ani hooków Craftsmanna (na innym profilu użytkownika albo na platformie bez zainstalowanego Craftsmanna) i wklej poniższy prompt (pozostawiony po angielsku, aby zachować pełną porównywalność z automatycznym baseline w `evals/run.py`):

```text
You are reviewing an uncommitted change in this git repository. The change description is in
`.git/evals/task.md`. See the change with `git status` and `git diff HEAD`; new files are
untracked, so read them too.

Find the real defects the change introduces or leaves behind. Read the code around the change,
not only the diff, and run the tests if that helps. Review only: do not modify any file in the
repository.

When you are done, write your findings to `.git/evals/findings.json` as a JSON list, one object
per defect, with exactly these keys:
  {"file": "<path relative to the repository root>", "line": <line number in the changed file>,
   "title": "<one sentence: what is wrong and when it shows>"}
Report each defect once, at the line where it is clearest. Also print the same list in your answer.
```

Jeśli agent nie może zapisać tego pliku, zapisz wypisaną przez niego listę JSON w dowolnym pliku i przekaż ją przez `--findings`.

### Claude Code, Antigravity i inni agenci

- **Claude Code**: przejdź (`cd`) do przygotowanego katalogu i uruchom `claude`. Dla ramienia z Craftsmanem użyj `/craft-review`; dla ramienia baseline wystartuj z profilu bez wtyczki Craftsmanna (na przykład z pustym `CLAUDE_CONFIG_DIR`) i wklej prompt bazowy.
- **Google Antigravity**: otwórz przygotowany katalog jako workspace. Ramię z Craftsmanem wymaga wtyczki lub `craftsman install --antigravity`; ramię baseline wymaga profilu bez nich. Porównuj ten sam model po obu stronach.
- **Każdy inny agent**: prompt baseline wymaga jedynie dostępu do powłoki i plików. Jeśli agent nie ma Craftsmanna, użyj ramienia baseline; zapisz jego listę JSON i oceń ją za pomocą `--findings`.

Zachowaj ten sam model, poziom wysiłku (effort) oraz zestaw narzędzi w obu ramionach — w przeciwnym razie porównujesz konfigurację środowiska zamiast protokołu.

## Ocena przebiegu

```bash
python3 -m evals.run score quotes ~/evals/quotes-1                    # rejestr lub .git/evals/findings.json
python3 -m evals.run score quotes ~/evals/quotes-1 --findings f.json  # lista baseline z dowolnej ścieżki
python3 -m evals.run score quotes ~/evals/quotes-1 --json             # wyjście maszynowe
```

Wyjście podaje recall dla danego przypadku (znalezione defekty / zasiane defekty), każdy defekt wraz ze znaleziskiem, które go wykryło, albo `MISSED`, znaleziska **niedopasowane (unmatched)** (możliwe fałszywe alarmy: warto je przeczytać, bo prawdziwy problem spoza zasianej listy też tam trafia), **trafienia w pułapki (decoy hits)** oraz duplikaty (dodatkowe znaleziska wskazujące defekt już wykryty).

Reguły dopasowania:

- Znalezisko zostaje przypisane do najbliższego defektu lub pułapki w swoim pliku. Odległość wynosi 0 wewnątrz przedziału `[start, end]` lokalizacji, a poza nim jest liczbą linii do bliższego końca; element o kilku lokalizacjach używa najbliższej z nich. Znalezisko zalicza się na poczet danego elementu tylko wtedy, gdy odległość wynosi najwyżej 5 linii — dzięki czemu znalezisko przesunięte o kilka wierszy nadal się liczy, a znalezisko bez żadnego elementu w promieniu 5 linii pozostaje niedopasowane. Remis rozstrzyga się na korzyść defektu; znalezisko leżące wewnątrz dwóch nakładających się zakresów przypisuje się węższemu z nich, dzięki czemu pułapka zagnieżdżona w zakresie defektu nadal jest zaliczana jako trafienie w pułapkę, jeśli zgłoszono jej własne linie. Ścieżki są porównywane względem katalogu głównego repozytorium (`./`, ukośniki wsteczne, ścieżki bezwzględne wewnątrz katalogu roboczego oraz litery dysków Windows są normalizowane).
- Znalezisko wskazujące własne linie pułapki jest trafieniem w pułapkę, nawet jeśli tuż obok znajduje się defekt.
- Każde znalezisko jest przypisywane co najwyżej jednemu defektowi lub pułapce, a każdy defekt liczy się raz: gdy kilka znalezisk wskazuje ten sam defekt, najbliższe go wykrywa, a pozostałe są duplikatami.
- **Precyzja** to odsetek punktowanych znalezisk wskazujących na zasiany defekt (wykryty lub duplikat). Jest to dolna granica: niedopasowane znalezisko może być prawdziwym błędem spoza listy zasianych defektów.
- Znaleziska z werdyktem `dismissed` są ignorowane. Z rejestru odczytywane są jedynie `findings[].file` (lub `.path`), `.line`, `.title` oraz `.verdict`, więc zmiany schematu rejestru nie mają znaczenia.
- Znaleziska pozbawione użytecznego pliku lub numeru linii są zliczane jako nieczytelne i pomijane.

Aby porównać ramiona ręcznie, oceń każde uruchomienie, następnie uśrednij wyniki recallu po powtórzeniach każdego ramienia i zestaw obok siebie niedopasowane znaleziska z obu ramion. Dla Claude Code robi to `compare` (uruchomienie bez rejestru ani pliku znalezisk liczy się jako zero znalezionych defektów i jest odpowiednio oznaczane w rekordzie).

## Dodawanie nowego przypadku

1. Utwórz `evals/cases/<id>/` zawierające:
   - `base/`: mała biblioteka lub CLI w Pythonie (tylko stdlib), maksymalnie około 200 linii wraz z testami, z testami `unittest` w `tests/` (w tym `tests/__init__.py`). Testy muszą działać po uruchomieniu `python3 -m unittest` z katalogu `base/`. **Duży** przypadek ma bazę wyglądającą jak prawdziwy projekt: od 6 do 9 modułów, plik testowy dla większości z nich oraz współdzielone helpery.
   - `head/`: to samo drzewo po zmianie obejmującej maksymalnie około 150 zmienionych linii, która realizuje `task.md`. Jego testy muszą przechodzić. Defekty muszą wyglądać na uczciwe pomyłki: bez komentarzy, nazw ani `TODO`, które by je zdradzały. Duży przypadek zmienia od 400 do 700 linii w 10–15 plikach (nowe moduły, edycje istniejących, nowe i zmienione testy) oraz zasiewa od 4 do 6 defektów — w tym co najmniej jeden `incomplete-propagation`, którego lokalizacje leżą w dwóch różnych plikach — plus od 2 do 4 pułapek. Pułapka musi leżeć w kodzie dotykanym przez zmianę i być poprawna z powodu wynikającego wprost z treści zadania lub kodu.
   - `task.md`: opis pull requesta, jaki otrzymuje recenzent. Opisz wymagania łamane przez defekty (defekt jest uczciwy tylko wtedy, gdy opis zadania albo otaczający kod mówi, co jest poprawne).
   - `truth.json`:

     ```json
     {
       "size": "large",
       "defects": [
         {"id": "x-1", "kind": "swallowed-error", "summary": "one line",
          "locations": [{"file": "pkg/mod.py", "start": 40, "end": 44, "anchor": "except OSError:"}]}
       ],
       "decoys": [
         {"id": "x-d1", "summary": "why this is correct",
          "locations": [{"file": "pkg/mod.py", "start": 10, "end": 12, "anchor": "if not items:"}]}
       ]
     }
     ```

     Pole `"size"` jest opcjonalne (domyślne to `"small"`; przypadek `"large"` sprawdzany jest pod kątem limitów dla dużych przypadków). `file` jest ścieżką względną wobec `head/`, numery linii są liczone od 1 i domknięte obustronnie. `anchor` to fragment tekstu występujący w jednej z linii `start..end` pliku w `head`; testy zawodzą, jeśli edycja pliku przesunie kod poza zadeklarowane linie. Trzymaj pułapki poza liniami defektów i pilnuj odstępu od sąsiednich defektów (zob. reguły dopasowania). Defekt propagacji można zgłosić zarówno w miejscu wywołującym, jak i wywoływanym: podaj oba w `locations`. `kind` jest jednym z powyższych rodzajów (`KINDS` w `run.py`).
2. Uruchom `python3 -m unittest tests.test_evals`. Test sprawdza, że testy `base` i `head` przechodzą, że każda lokalizacja wskazuje na istniejącą, niepustą linię, że rozmiary mieszczą się w limitach dla zadeklarowanego `size`, że `evals/README.md` nadal zawiera prompt bazowy z `run.py` oraz że zestaw ma co najmniej 12 defektów pokrywających wszystkie rodzaje. Testy `agent` i `compare` używają atrapy `claude` i nigdy nie wywołują prawdziwego CLI.
3. Dopisz przypadek do powyższej tabeli. Zestaw nie ma limitu liczby defektów; ogranicz jednak liczbę dużych przypadków, bo to one podnoszą koszt uruchomień.

Linting: `python3 -m ruff check evals tests/test_evals.py tests/test_evals_usage.py` oraz `python3 -m mypy --strict --exclude 'evals/cases/' evals tests/test_evals.py tests/test_evals_usage.py` przechodzą czysto (drzewa przypadków to fixtury z zasianymi defektami i nie podlegają lintowaniu).

---

## Benchmark build-and-verify (`evals.build_bench`)

Uzupełniający zestaw zadań kodujących w `evals/build_cases/` (`6` wielomodułowych projektów Pythona opartych wyłącznie o stdlib), służący do porównywania pełnej implementacji z bramką `/craftsman` przeciwko nieuzbrojonemu wariantowi baseline na podstawie ukrytych testów `hidden_tests.py` niewidocznych w workspace agenta.

| Przypadek | Pakiet | Ukryte testy | Co sprawdza |
| :--- | :--- | :---: | :--- |
| `ledger_batch` | `ledger_batch` | 10 | Podwójny zapis księgowy, atomowy rollback partii, konflikt vs. powtórka idempotencji, storna, bilans próbny |
| `jobqueue_retry` | `jobqueue_retry` | 10 | Priorytety i `seq`, wygaszanie dzierżaw, ochrona przed spóźnionymi workerami, wykładniczy backoff, DLQ, anulowanie, JSON |
| `config_overlay` | `config_overlay` | 9 | Dziedziczenie diamentowe, dyrektywy `$merge`/`$unset`, interpolacja z detekcją cykli i escapowaniem `\${...}`, walidacja i redakcja sekretów |
| `docindex_search` | `docindex_search` | 9 | Czyszczenie indeksów przy update/delete, offsety po stopwordach, frazy pozycyjne, operatory logiczne, wielokrotne fasety, migawki |
| `pkgcache_sync` | `pkgcache_sync` | 9 | Weryfikacja SHA-256, atomowy staging, przechodnie pinowanie zależności, transakcyjny limit pojemności, deduplikacja i GC |
| `schedule_rrule` | `schedule_rrule` | 8 | Deterministyczne reguły DST w stdlib, `DAILY`/`WEEKLY`/`MONTHLY` (`skip`/`clamp`), `COUNT`/`EXDATE`/`RECURRENCE-ID`, konflikty |

```bash
python3 -m evals.build_bench list
python3 -m evals.build_bench prepare ledger_batch ~/evals/ledger-1   # kopiuje tylko starter/ i TASK.md (bez hidden_tests.py)
python3 -m evals.build_bench grade ledger_batch ~/evals/ledger-1     # uruchamia tests/test_starter.py oraz ukryte hidden_tests.py
python3 -m evals.build_bench summary bench/build_eval.jsonl          # podsumowuje wyniki JSONL per ramię i przypadek
python3 -m unittest tests.test_build_cases                           # weryfikuje projekty startowe, ukryte testy, LF oraz brak przecieków
```
