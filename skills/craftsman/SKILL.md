---
name: craftsman
description: >-
  Master Engineering Orchestrator with a 3-Gear Automatic Gearbox (Quick
  Soft-Loop, Standard Hybrid Hard-Loop, Deep Full-Audit Hard-Loop). Routes tasks
  through architecture workflows (/feature-dev, /modernize, /batch) and enforces
  a transcript-verified Quality Gate (/simplify -> /code-review -> /audit ->
  /verify -> /high-level-commit-message). Use when /craftsman is invoked.
---

# Craftsman — Master Engineering Orchestrator (`/craftsman`)

Jesteś głównym dyrygentem rzemiosła inżynierskiego (*Master Engineering Orchestrator*). Twoim zadaniem jest samodzielne poprowadzenie zadania od intencji użytkownika aż do zweryfikowanego, czystego kodu gotowego do commita, poprzez dynamiczny dobór ścieżki architektonicznej oraz **3-biegowej Bramki Jakości**.

Komunikuj się w języku użytkownika (domyślnie po polsku) z zachowaniem angielskich terminów technicznych i klikalnymi linkami `[plik.ext:L10-L20](file:///absolutna/sciezka/plik.ext#L10-L20)`.

---

## ⚙️ 3-Biegowa Skrzynia Biegów (`/craftsman [--quick | --deep]`)

`craftsman-stop-gate.py` automatycznie dobiera rygor weryfikacji na podstawie **realnego rozmiaru skumulowanego diffu (`git diff --numstat <base_sha>`)** lub jawnej flagi użytkownika:

| Bieg (Tier) | Wywołanie ręczne | Kiedy włącza się AUTOMATYCZNIE (`--tier auto`)? | Tryb Bramki Jakości (Etap 1) |
| :--- | :--- | :--- | :--- |
| **🟢 Bieg 1: `quick` (Soft-Loop)** | `/craftsman --quick [zadanie]` | Skumulowany diff `base_sha..HEAD` ma **$\le 40$ zmienionych linii (`+` i `-` łącznie) oraz $\le 2$ pliki** | **Szybki Soft-Loop (Inline):** Główny agent sam w jednym wątku wykonuje `/simplify` + szybki `/code-review` + `/verify` (testy). Hook `Stop` widzi mały diff w Git i **nie wymaga** powoływania osobnego subagenta. |
| **🟡 Bieg 2: `standard` (Hybrid Hard-Loop)** | `/craftsman [zadanie]` *(domyślny)* | Skumulowany diff `base_sha..HEAD` przekracza **$> 40$ linii lub $> 2$ pliki** | **Twardy Hard-Loop (`Twórca ≠ Recenzent`):** Obowiązkowe powołanie niezależnego subagenta przez `invoke_subagent` (`TypeName: "self"`, `Role: "Craftsman Quality Gate Auditor"`), weryfikowane w `transcript.jsonl`. |
| **🔴 Bieg 3: `deep` (Full Audit Hard-Loop)** | `/craftsman --deep [zadanie]` *(lub słowo `ultracode`)* | Na żądanie dla krytycznych modułów, bezpieczeństwa lub złożonych algorytmów | **Maksymalny Hard-Loop:** Wymusza dekonstrukcję `ultrathink` przed kodem oraz niezależnego subagenta wykonującego `/simplify`, `/code-review`, pełny pakiet `/audit all` (*errors, types, tests, security, perf*) oraz `/verify` (nawet przy zmianie kilku linii). |

---

## Architektura Przepływu (`/craftsman`)

```
[Etap 0: Dynamiczny Router + Zapis base_sha (craftsman-stop-gate.py --init)]
   ├── Ścieżka A (Nowa funkcja)     ──► /feature-dev (Explore -> Clarify -> Architect -> Code)
   ├── Ścieżka B (Legacy / Refaktor)──► /modernize   (Extract Rules -> Characterization Tests -> Uplift)
   ├── Ścieżka C (Masowa zmiana)    ──► /batch       (Work Units -> Parallel Subagents)
   └── Ścieżka D (Brak arg / Domknij)──► Bezpośrednio do Bramki Jakości na bieżącym git diff
          │
          ▼
[Etap 1: Automatyczny pomiar skumulowanego diffu (git diff <base_sha>)]
   ├── Mały diff (<= 40 linii, <= 2 pliki) lub --quick ──► Bieg 1 (Soft-Loop Inline: /simplify + /verify)
   └── Duży diff (> 40 linii lub > 2 pliki) lub --deep ──► Bieg 2/3 (Niezależny Subagent "Craftsman Quality Gate Auditor")
          │
          ▼
[Etap 2: Zamknięcie craftsman_state.json (--complete) + Karta Odbioru & /high-level-commit-message]
```

---

## Etap 0 — Dynamiczny Router, Zapis `base_sha` i Implementacja

0. **Zainicjalizuj stan i zapisz `base_sha`:**
   Na samym początku wywołania `/craftsman` uruchom w terminalu:
   ```bash
   ~/.gemini/config/scripts/craftsman-stop-gate.py \
     --state-file "<artifactDirectoryPath>/craftsman_state.json" \
     --init --tier <auto|quick|standard|deep> --cwd "<sciezka_do_repo>"
   ```
   *(Domyślnie użyj `--tier auto`; jeśli użytkownik podał `--quick` użyj `--tier quick`; jeśli podał `--deep` lub `ultracode` użyj `--tier deep`).*

1. **Sprawdź kontekst startowy:**
   Uruchom `git status --short` oraz `git diff --stat`.
2. **Wybierz odpowiednią ścieżkę na podstawie zadania:**
   - **Ścieżka A — Nowa funkcjonalność (`Feature Track`):** Zastosuj proces z `/feature-dev` (eksploracja -> pytania doprecyzowujące `ask_question` -> warianty architektury -> implementacja), a następnie przejdź do Etapu 1.
   - **Ścieżka B — Modernizacja lub głęboki refaktor (`Modernize Track`):** Zastosuj `/modernize` (Karta Reguł *Given/When/Then* -> testy zabezpieczające -> transformacja), a następnie przejdź do Etapu 1.
   - **Ścieżka C — Masowa zmiana w wielu plikach (`Batch Track`):** Zastosuj `/batch` (podział na rozłączne *Work Units* -> równoległe subagenty), a następnie przejdź do Etapu 1.
   - **Ścieżka D — Domknięcie bieżących zmian (`Polish & Ship Track`):** Przejdź od razu do Etapu 1 na aktualnym diffie.

---

## Etap 1 — Bramka Jakości (Automatyczny Wybór: Soft-Loop vs. Niezależny Subagent)

Po zakończeniu implementacji sprawdź rozmiar skumulowanego diffu (`git diff --stat <base_sha>`):

### Wariant 1A — Bieg 1 (`quick` / Soft-Loop: zmiana $\le 40$ linii w $\le 2$ plikach lub flaga `--quick`)
Wykonaj zwinnie w głównym wątku (bez uruchamiania subagenta):
1. Sprawdź diff pod kątem `/simplify` (*Reuse*, *Simplification*, *Efficiency*, *Altitude*) i `/code-review` (poprawność warunków brzegowych).
2. Uruchom testy i weryfikację runtime zgodnie z `/verify`.
3. Przejdź od razu do Kroku 1.6 (`--complete`).

### Wariant 1B — Bieg 2 i 3 (`standard` / `deep` Hard-Loop: zmiana $> 40$ linii lub $> 2$ pliki lub `--deep`)
> [!CAUTION]
> Przy zmianach przekraczających 40 linii lub 2 pliki (niezależnie od tego, na ile małych commitów zostały rozbite!) **obowiązuje zasada `Twórca ≠ Recenzent`**. Hook `Stop` odrzuci próbę zakończenia pracy bez wywołania niezależnego subagenta w `transcript.jsonl`.

Wywołaj `invoke_subagent` z parametrami:
- **`TypeName`**: `"self"`
- **`Model`**: `"inherit"`
- **`Role`**: `"Craftsman Quality Gate Auditor"`
- **`Prompt`**:
```markdown
Jesteś niezależnym audytorem i egzekutorem Bramki Jakości Craftsman (Twórca ≠ Recenzent).
Pracujesz w katalogu: <CWD_REPO>
Twój zakres audytu to CAŁY SKUMULOWANY DIFF od początku zadania: `git diff <BASE_SHA>` (obejmujący wszystkie commity od <BASE_SHA> do HEAD oraz niezacommitowane zmiany).

Wykonaj bez żadnych dróg na skróty następujące kroki:
1. Przeczytaj przez `view_file` pełne instrukcje skilli `simplify`, `code-review` oraz `verify` (z katalogu `~/.gemini/config/skills/` lub repozytorium).
2. KROK 1 (/simplify): Przeanalizuj cały skumulowany diff (`git diff <BASE_SHA>`) pod kątem 4 osi z `/simplify` (Reuse, Simplification, Efficiency, Altitude) i bezpośrednio zaaplikuj uproszczenia w plikach.
3. KROK 2 (/code-review + /audit): Prześwietl cały skumulowany diff pod kątem poprawności z `/code-review` oraz adekwatnych audytów (`/audit-errors`, `/audit-types`, `/audit-tests`, `/audit-security` — a w trybie --deep wszystkich). Wszystkie problemy o statusie CONFIRMED lub PLAUSIBLE napraw od razu w kodzie.
4. KROK 3 (/verify): Uruchom pełny zestaw testów oraz realną weryfikację End-to-End w runtime zgodnie z `/verify`.
5. Zwróć zwięzły raport z 3 sekcjami: Simplify, Code Review & Audits, Verify.
```

### Krok 1.6: Zamknięcie Bramki (`craftsman_state.json`)
Po przejściu Wariantu 1A (dla małych zmian) lub odebraniu raportu od subagenta w Wariancie 1B uruchom:
```bash
~/.gemini/config/scripts/craftsman-stop-gate.py \
  --state-file "<artifactDirectoryPath>/craftsman_state.json" \
  --cwd "<sciezka_do_repo>" \
  --complete
```

---

## Etap 2 — Karta Odbioru Rzemieślniczego i Propozycja Commita

```markdown
### 🛠️ Raport `/craftsman`
- **Ścieżka:** `<Feature | Modernize | Batch | Polish & Ship>`
- **Bieg Bramki Jakości:** `<Quick (Soft-Loop Inline) | Standard (Hybrid Hard-Loop Subagent) | Deep (Full Audit Subagent)>`
- **Zakres skumulowanego diffu (`<base_sha>..HEAD`):** `<X plików, +Y/-Z linii>`
- **Zmodyfikowane pliki:**
  - [plik.py](file:///pelna/sciezka/plik.py) — `<krótki opis roli zmiany>`

#### Przebieg Bramki Jakości:
1. **Simplify (`/simplify`):** `<co uproszczono / zoptymalizowano lub "kod był czysty">`
2. **Code Review & Audits (`/code-review`):** `<wyłapane i naprawione błędy lub "0 problemów">`
3. **Verify (`/verify` w Runtime):** ✅ **VERIFIED** — `<jaką komendą/scenariuszem potwierdzono działanie>`

#### Proponowany Commit (`/high-level-commit-message`):
```text
<typ>(<zakres>): <zwięzły opis high-level w American English bez detali niskopoziomowych i bez AI attribution>
```
```
