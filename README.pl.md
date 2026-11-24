![Craftsman](assets/logo.svg)

# Craftsman

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/craftsman-gate)](https://pypi.org/project/craftsman-gate/)
[![Version](https://img.shields.io/badge/version-5.2.0-emerald.svg)](plugin.json)
[![Agent Skills](https://img.shields.io/badge/Agent_Skills-skills.sh-0F766E.svg)](docs/skills.md)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10--3.13-blue.svg)](pyproject.toml)
[![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Agenci kodujący AI zwykle sami oceniają swoją pracę i kończą, gdy tylko wszystko wygląda na zielono. Craftsman pozwala agentowi **kodować swobodnie, po czym wymusza surową recenzję**: gdy agent uzna zadanie za gotowe, Craftsman zamraża diff, wysyła na zmianę świeżokontekstowych łowców błędów oraz weryfikatorów próbujących obalić zgłoszenia, zapisuje werdykty i realne uruchomienia testów w `.git/`, a hookami platformy blokuje zakończenie do czasu naprawy potwierdzonych błędów i przejścia testów.

---

## Instalacja

Wymaga Pythona 3.10+ bez zewnętrznych zależności (tylko `stdlib`).

```bash
pipx install craftsman-gate        # lub: uv tool install craftsman-gate
craftsman install                  # wykrywa Claude Code, Copilot i Antigravity
```

Albo jako wtyczka platformowa:

```bash
/plugin marketplace add BuczynskiRafal/craftsman              # Claude Code
/plugin install craftsman@craftsman
agy plugin install https://github.com/BuczynskiRafal/craftsman   # Antigravity
codex plugin marketplace add BuczynskiRafal/craftsman            # Codex
codex plugin add craftsman@craftsman
```

Cursor instaluje wtyczkę z lokalnego checkoutu — zobacz [konfiguracja wtyczki Cursor](docs/installation.md#cursor-plugin).

### Wypróbuj skille bez Pythona

Wszystkie 13 skilli jest zgodnych ze standardem [Agent Skills](https://agentskills.io) i instaluje się bezpośrednio przez `skills.sh`:

```bash
npx skills add BuczynskiRafal/craftsman
# albo wybierz konkretne skille:
npx skills add BuczynskiRafal/craftsman --skill audit-security --skill simplify
```

Jedenaście skilli workflowowych i audytowych działa w pełni samodzielnie bez Pythona, a `/craft-review` działa samodzielnie w trybie bez bramki. Zainstaluj `craftsman-gate` powyżej, jeśli chcesz mieć deterministyczne plany polowania, rejestr dowodów w `.git` i hooki Stop blokujące przedwczesne zakończenie. Zobacz [katalog skilli](docs/skills.md) i [przewodnik instalacji](docs/installation.md).

## Użycie

Opisz zadanie zwykłym językiem — bez pamiętania flag:

```text
/craftsman add pagination to the catalog search
/craftsman migrate the session store to Redis, this is critical
/craft-review review my staged changes and fix what you find
/craft-review review PR 42
/craft-review be thorough, this touches authentication
```

Agent sam określa zakres, to, czy ma wprowadzać poprawki, oraz głębokość recenzji (automatycznie wchodząc głębiej przy sygnałach ryzyka lub słowach takich jak `critical` i `ultracraft`; zob. sekcję **Depth** w [`craft-review`](skills/craft-review/SKILL.md) i [docs/usage.md](docs/usage.md)). W każdej chwili możesz uruchomić `craftsman status`, żeby zobaczyć pokrycie i brakujące dowody.

## Jak to działa

| | Zwykły `CLAUDE.md` / prompt | Zewnętrzne boty PR | Craftsman |
| :--- | :--- | :--- | :--- |
| **Kiedy działa** | Tylko jeśli agent pamięta | Po otwarciu PR-a | Zanim agent skończy, na lokalnym diffie |
| **Kto sprawdza** | Ten sam kontekst, który napisał błąd | Pojedynczy zdalny przegląd PR-a | Świeżokontekstowi łowcy + weryfikatorzy obalający zgłoszenia |
| **Dowód gotowości** | Agent twierdzi, że testy przeszły | Komentarze na GitHubie | Lokalny rejestr w `.git` z pokrytymi hunkami, werdyktami i przejściami `craftsman run` |
| **Setup** | Sam tekst promptu | Usługa chmurowa / webhook | CLI w Pythonie (tylko stdlib) albo samodzielne Agent Skills |

![Craftsman 5: build freely, review hard](docs/diagrams/harness-overview.png)

1. **Zamrożenie i plan.** Diff zostaje zamrożony i podzielony według rozmiaru oraz sygnałów ryzyka na obowiązkowe porcje i soczewki.
2. **Polowanie.** Agenci ze świeżym kontekstem badają każdą porcję i zapisują podejrzewane błędy wraz z konkretnym wyzwalaczem.
3. **Weryfikacja.** Osobni weryfikatorzy próbują obalić każde podejrzenie i odtwarzają te, które przetrwają; potwierdzone błędy uruchamiają kolejne fale.
4. **Poprawki i testy.** Poprawki u źródła są ponownie sprawdzane przez świeżych łowców, a `craftsman run` potwierdza przejście testów na końcowym kodzie.

`/craft-review` wykonuje kroki 1–3 na istniejącej zmianie i poprawia kod wyłącznie wtedy, gdy o to poprosisz. Pełny protokół: [docs/how-it-works.md](docs/how-it-works.md).

## Jak wygląda bramka w akcji

Zarejestrowane na [demie paginacji katalogu](examples/demo/README.pl.md) (`./setup.sh` w systemach POSIX, `.\setup.ps1` w Windows):

**1. Próba przedwczesnego zakończenia uruchamia hook Stop:**

```text
[CRAFTSMAN GATE 1/6] review incomplete: no passing test run; no reviewer isolation evidence. Task completion is blocked until the evidence ledger proves the change is reviewed and verified.
Next actions:
1. [no-isolation] No Craftsman subagent was recorded for round 1. ...
2. [no-passing-run] No passing test or e2e run at the current code; run `craftsman run --kind test -- <your test command>`.
Close the gate with `craftsman gate --complete` once `craftsman review check` passes.
```

**2. Po weryfikacji, poprawkach i testach `craftsman review report` pokazuje rejestr:**

```text
$ craftsman review report
- Base `7a8b04a` · 2 review round(s) · 2 files, +48/−10 lines
- Gate: PASSED

## Findings
**Fixed (2)**
- F1 · major · `shop/catalog.py:37` — total_pages truncates partial pages via floor division. Fix: Used ceiling division; confirmed by test_list_products_partial_last_page (R3).
- F2 · major · `shop/catalog.py:36` — page < 1 uses negative slice indices instead of raising ValueError. Fix: Added page >= 1 and page_size >= 1 checks; confirmed by test_list_products_rejects_non_positive_page (R3).

## Verification at the final code
- R3 `python -m unittest discover -s tests` (test) → exit 0 in 0.2s
```

## Platformy

| Platforma | Co blokuje zakończenie |
| :--- | :--- |
| Claude Code | Hook `Stop` |
| GitHub Copilot w VS Code | Hook `Stop` |
| GitHub Copilot CLI | Hook `agentStop` |
| Google Antigravity | Hook `Stop` |
| Codex, Cursor i narzędzia z `AGENTS.md` | `craftsman gate --complete` |

Tam, gdzie środowisko nie udostępnia hooków subagentów (Codex, Cursor lub sesja bez hooków), recenzja po pokryciu każdego hunka ma charakter doradczy i oznacza raport jako `SELF-REVIEWED`.

## Pomiary

Zmierzone na modelu `gemini-3.8-flash-low` w dwóch zestawach A/B pod Antigravity z natywnymi `hooks.json` na Windows (`n=2` przebiegi na ramię, `0` błędów hooków, `28/28` wymuszonych rejestrów):

- **Recenzja z zasianymi defektami ([`evals/`](evals/README.pl.md), 8 przypadków / 27 błędów / 18 pułapek):** `89%` (`24.00/27`) bez wsparcia → **`98%`** (`26.50/27`, precyzja **`84%`**) w `5.2.0`, z wykryciem wszystkich 3 defektów pominiętych dwukrotnie przez baseline (`inv-2`, `od-6`, `tb-3`) oraz redukcją subagentów o **47%** (`208.0 → 111.0`), niedopasowanego szumu o **45%** (`29.00 → 16.00`) i tokenów o **22%** (`111.3M → 86.6M` na przebieg, `×26.1` wobec `3.3M` w baseline) względem `5.1.0`.
- **Build-and-verify (`evals/build_cases/`, 6 wielomodułowych zadań / 110 ukrytych testów):** `66.7%` pełnych zaliczeń / `96.4%` ukrytych testów (`8/12`, `106/110`, `404k` tokenów, `2.9 min`) w baseline → **`75.0%`** pełnych zaliczeń / **`97.3%`** ukrytych testów (`9/12`, `107/110`; **`80.0%`** / **`98.0%`** w przebiegach `ok`) w `5.2.0`, przy poprawie `49` zgłoszeń, średnio `21.7M` tokenów (`12.8M` `ok`, `-23%`/`-39%` względem `00a8fab`) oraz `7/12` timeoutach na limicie `40 min` w trakcie dogrywek drugiej rundy (`5` już po zaliczeniu `100%` ukrytych testów).

Wniosek: do rutynowych zadań greenfield lepiej używać samych skilli z `skills.sh`, a pełną bramkę Stop zachować dla ryzykownych diffów, migracji legacy i krytycznych recenzji. Pełne tabele i ograniczenia: [docs/benchmark.md](docs/benchmark.md).

## Dokumentacja

- [Użycie](docs/usage.md) · [Jak to działa](docs/how-it-works.md) · [Instalacja](docs/installation.md) · [Strażnicy](docs/guards.md) · [Skille](docs/skills.md) · [Benchmark](docs/benchmark.md) · [Demo](examples/demo/README.pl.md) · [Architektura](docs/harness-architecture.md)

## Współpraca i licencja

Współpraca: [CONTRIBUTING.md](CONTRIBUTING.md) · Cytowanie: [CITATION.cff](CITATION.cff) · Licencja: MIT ([LICENSE](LICENSE), [NOTICE](NOTICE)).
