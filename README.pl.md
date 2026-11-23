![Craftsman](assets/logo.svg)

# Craftsman

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/craftsman-gate)](https://pypi.org/project/craftsman-gate/)
[![Version](https://img.shields.io/badge/version-5.1.0-emerald.svg)](plugin.json)
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

Benchmark A/B na zasianym zestawie defektów w [`evals/`](evals/README.pl.md) (`8` przypadków, `27` defektów, `18` pułapek) na `gemini-3.8-flash-low` przez Antigravity (`agy -p`, Craftsman `5.1.0` vs. baseline bez wsparcia, `2` pełne przebiegi = `32` uruchomienia):

- **Skuteczność wykrywania (recall)**: `89%` (`24.00/27`) bez Craftsmanna → **`98%`** (`26.50/27`, `27/27` w 2. przebiegu, `+9.3 pkt`, 90% przedział Welcha `-2.4 do +21.0 pkt`), z wykryciem wszystkich trzech defektów pominiętych przez baseline w obu przebiegach (`inv-2`, `od-6`, `tb-3`).
- **Kompromisy**: precyzja `100%` → `83%` (`1.50` trafień w pułapki i `29.00` niedopasowanych znalezisk na przebieg), czas `11.9` → `144.1 min`/przebieg (`×12.14`, w tym dwa przekroczenia limitu 30 min w 1. przejściu), tokeny `3.3M` → `111.3M`/przebieg (`×33.54`, `84.8%` w subagentach).

Najbardziej opłaca się przy trudnych zmianach, migracjach legacy i kodzie o wysokich wymaganiach niezawodności — zbędne przy szybkich prototypach i drobnych poprawkach. Pełne tabele i ograniczenia: [docs/benchmark.md](docs/benchmark.md).

## Dokumentacja

- [Użycie](docs/usage.md) · [Jak to działa](docs/how-it-works.md) · [Instalacja](docs/installation.md) · [Strażnicy](docs/guards.md) · [Skille](docs/skills.md) · [Benchmark](docs/benchmark.md) · [Demo](examples/demo/README.pl.md) · [Architektura](docs/harness-architecture.md)

## Współpraca i licencja

Współpraca: [CONTRIBUTING.md](CONTRIBUTING.md) · Cytowanie: [CITATION.cff](CITATION.cff) · Licencja: MIT ([LICENSE](LICENSE), [NOTICE](NOTICE)).
