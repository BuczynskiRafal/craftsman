![Craftsman](assets/logo.svg)

# Craftsman

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/craftsman-gate)](https://pypi.org/project/craftsman-gate/)
[![Version](https://img.shields.io/badge/version-5.2.0-emerald.svg)](plugin.json)
[![Agent Skills](https://img.shields.io/badge/Agent_Skills-skills.sh-0F766E.svg)](docs/skills.md)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10--3.13-blue.svg)](pyproject.toml)
[![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Craftsman zamyka agenta kodującego AI w pętli jakości: **koduj swobodnie, recenzuj surowo**. Agent pisze kod normalnie, a dopiero gdy uzna zadanie za gotowe, Craftsman zamraża diff, wysyła świeżokontekstowych łowców błędów i weryfikatorów próbujących obalić zgłoszenia, zapisuje werdykty oraz realne testy w `.git/`, a hookami platformy blokuje zakończenie do czasu naprawy potwierdzonych błędów i przejścia testów.

Działa z Claude Code, GitHub Copilot i Google Antigravity dzięki hookom `Stop`, a w Codexie i Cursorze bez hooków.

## Jak to działa

![Craftsman 5: build freely, review hard](docs/diagrams/harness-overview.png)

1. **Zamrożenie i plan.** Diff zostaje zamrożony i podzielony według rozmiaru oraz sygnałów ryzyka na obowiązkowe porcje i soczewki.
2. **Polowanie na błędy.** Każda porcja trafia do osobnego agenta ze świeżym kontekstem, który zapisuje podejrzewane błędy wraz z konkretnym wyzwalaczem.
3. **Weryfikacja.** Osobni weryfikatorzy próbują obalić każde podejrzenie i odtwarzają te, które przetrwają; potwierdzone błędy uruchamiają kolejne fale.
4. **Poprawki i testy.** Poprawki u źródła są ponownie sprawdzane przez świeżych łowców, a testy muszą przejść na końcowym kodzie przez `craftsman run`.

`/craft-review` wykonuje tę samą recenzję na istniejącej zmianie i poprawia kod tylko wtedy, gdy o to poprosisz. Pełny protokół: [docs/how-it-works.md](docs/how-it-works.md) · Przykład krok po kroku: [demo paginacji katalogu](examples/demo/README.pl.md).

## Gdzie sprawdza się najlepiej

Craftsman najlepiej sprawdza się przy:

- nietrywialnych zmianach i trudnych poprawkach,
- refaktorach i migracjach starszego kodu,
- zmianach dotykających bezpieczeństwa, współbieżności lub publicznych kontraktów,
- kodzie, w którym ciche błędy są kosztowne.

Przy prototypach i drobnych zadaniach greenfield jest zbędnym narzutem — do lekkich codziennych sprawdzeń możesz zainstalować pojedyncze skille samodzielnie przez `skills.sh` poniżej.

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

Przetestowane na modelu `gemini-3.8-flash-low` przez Antigravity CLI (po 2 pełne przejścia):

| Co sprawdzaliśmy | Bez Craftmana | Z Craftsmanem 5.2 |
| :--- | :--- | :--- |
| **Szukanie błędów w gotowych PR-ach** ([`evals/`](evals/README.pl.md))<br>8 repozytoriów, 27 ukrytych błędów, zielone testy | Wykryto **24 / 27** błędów (`89%`)<br>`12 min` · `3.3M` tokenów | Wykryto **26.5 / 27** błędów (**`98%`**)<br>`143 min` · `86.6M` tokenów |
| **Pisanie funkcji od zera** (`evals/build_cases/`)<br>6 wieloplikowych zadań ocenianych ukrytymi testami | **8 / 12** przejść na 100% (`67%`)<br>`3 min` · `404k` tokenów | **9 / 12** przejść na 100% (**`75%`**)<br>`34 min` · `21.7M` tokenów\* |

*\*W teście pisania od zera `7 / 12` uruchomień Craftmana doszło do limitu 40 minut w trakcie ponownego sprawdzania poprawek (`5` z nich już wcześniej przechodziło `100%` ukrytych testów). Pełne wyniki i ograniczenia: [docs/benchmark.md](docs/benchmark.md).*

## Instalacja

Wymaga Pythona 3.10+ bez zewnętrznych zależności (tylko `stdlib`):

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

Albo zainstaluj same skille Agent Skills bez Pythona (`11` działa samodzielnie; `/craft-review` działa bez bramki):

```bash
npx skills add BuczynskiRafal/craftsman
```

## Użycie

Opisz zadanie zwykłym językiem — bez pamiętania flag:

```text
/craftsman add pagination to the catalog search
/craftsman migrate the session store to Redis, this is critical
/craft-review review my staged changes and fix what you find
/craft-review review PR 42
/craft-review be thorough, this touches authentication
```

Agent sam określa zakres, to, czy ma wprowadzać poprawki, oraz głębokość recenzji (wchodząc głębiej przy sygnałach ryzyka lub słowach takich jak `critical` i `ultracraft`; zob. [docs/usage.md](docs/usage.md) i sekcję **Depth** w [`craft-review`](skills/craft-review/SKILL.md)). W każdej chwili możesz uruchomić `craftsman status`, żeby zobaczyć pokrycie i brakujące dowody.

## Dokumentacja

- [Użycie](docs/usage.md) · [Jak to działa](docs/how-it-works.md) · [Instalacja](docs/installation.md) · [Strażnicy](docs/guards.md) · [Skille](docs/skills.md) · [Benchmark](docs/benchmark.md) · [Demo](examples/demo/README.pl.md) · [Architektura](docs/harness-architecture.md)

## Współpraca i licencja

Współpraca: [CONTRIBUTING.md](CONTRIBUTING.md) · Cytowanie: [CITATION.cff](CITATION.cff) · Licencja: MIT ([LICENSE](LICENSE), [NOTICE](NOTICE)).
