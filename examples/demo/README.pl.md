# Demo Craftsmanna: Paginacja katalogu

Minimalny projekt demonstracyjny pokazujący, jak Craftsman powstrzymuje agenta kodującego AI przed ogłoszeniem „gotowe” przy niekompletnej albo wadliwej implementacji.

## Zadanie

Powiedz to zwykłymi słowami:
> *"Add 1-based pagination to catalog search results (page number and optional page size)."*
*(„Dodaj 1-indeksowaną paginację wyników wyszukiwania w katalogu (numer strony oraz opcjonalny rozmiar strony).”)*

### Dlaczego agenci zwykle popełniają tu błędy
Paginacja sprzyja subtelnym błędom brzegowym:
1. **Obcinanie niepełnej ostatniej strony**: `total_pages = len(products) // size` ucina resztę z dzielenia (25 pozycji przy rozmiarze strony 20 daje 1 stronę zamiast 2; 5 pozycji daje 0 stron).
2. **Brak walidacji numeru strony**: `start = (page - 1) * size` bez sprawdzenia `page >= 1`. W Pythonie `page = 0` tworzy ujemne indeksy wycinka (`products[-20:0]`), zwracając błędne pozycje zamiast błędu lub pustego wyniku.

## Co robi Craftsman

1. **Swobodne kodowanie.** `/craftsman` zapisuje commit bazowy; agent implementuje funkcję i uruchamia własne testy. Craftsman niczego w tym czasie nie recenzuje.
2. **Zamrożenie i plan.** Gdy agent uzna funkcję za gotową, diff zostaje zamrożony i podzielony na porcje do sprawdzenia (sloty), każda z kilkoma perspektywami badania (soczewkami takimi jak `contracts`, `intent`, `failure`, `tests`).
3. **Polowanie na błędy.** Każda porcja trafia do osobnego agenta ze świeżym kontekstem, który nie pisał tego kodu. Zapisuje on każdy podejrzewany błąd wraz z konkretną sytuacją wyzwalającą, taką jak `page=0` albo 25 pozycji przy rozmiarze strony 20.
4. **Weryfikacja.** Osobni agenci próbują obalić każde podejrzenie, a te, które przetrwają, odtwarzają za pomocą `craftsman run --kind repro`, udowadniając błędy brzegowe na prawdziwym kodzie. Ponieważ ta fala potwierdziła defekty, uruchamiana jest kolejna: szuka tych samych błędów w innych miejscach i wykonuje jedno świeże spojrzenie bez wiedzy o wcześniejszych znaleziskach.
5. **Poprawki i testy.** Agent naprawia błędy brzegowe u źródła i dodaje testy regresyjne, które zawodzą przed poprawką i przechodzą po niej. Świeżokontekstowy łowca ponownie sprawdza poprawki, agent uruchamia `craftsman run --kind test`, a bramka przepuszcza zadanie. Raport podaje, co znaleziono, naprawiono i odrzucono oraz dlaczego agent wybrał taką głębokość.

Jeśli agent spróbuje zakończyć przed wykonaniem wszystkich tych kroków, hook Stop zawróci go z listą braków:

```
[CRAFTSMAN GATE 1/6] review incomplete: 2 hunks not covered; 1 finding without a verdict. Task completion is blocked until the evidence ledger proves the change is reviewed and verified.
```

## Szybki start

Przygotuj odizolowane repozytorium demo:

**macOS / Linux / Git Bash:**

```bash
./setup.sh /tmp/craftsman-demo
cd /tmp/craftsman-demo
```

**Windows (PowerShell):**

```powershell
.\setup.ps1 "$env:TEMP\craftsman-demo"
cd "$env:TEMP\craftsman-demo"
```

Następnie w Claude Code, GitHub Copilot albo Antigravity zleć zadanie własnymi słowami:

```text
/craftsman Add 1-based pagination to catalog search results (page number and optional page size)
```

Aby zobaczyć wyłącznie samą recenzję, zaimplementuj funkcję samodzielnie i poproś:

```text
/craft-review review my changes and fix what you find
```
