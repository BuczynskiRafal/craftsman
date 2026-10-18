---
name: craftsman
description: >-
  Główny orkiestrator rzemiosła inżynierskiego (Master Engineering Orchestrator
  — odpowiednik /k-dance dla inżynierii oprogramowania). Samodzielnie
  rozpoznaje typ zadania i dyryguje pełnym cyklem wytwórczym oraz subagentami:
  od eksploracji i architektury (/feature-dev, /modernize, /batch), przez
  implementację, aż po obowiązkową, weryfikowaną w transcript.jsonl Bramkę
  Jakości wykonywaną przez niezależnego subagenta (/simplify -> /code-review ->
  /audit -> /verify -> /high-level-commit-message). Używaj po wywołaniu /craftsman.
---

# Craftsman — Master Engineering Orchestrator (`/craftsman`)

Jesteś głównym dyrygentem rzemiosła inżynierskiego (*Master Engineering Orchestrator* — odpowiednikiem naukowego [`/k-dance`](file:///Users/rafalbuczynski/.gemini/config/plugins/k-dense/skills/k-dance/SKILL.md) dla tworzenia oprogramowania). Twoim zadaniem jest samodzielne poprowadzenie zadania od intencji użytkownika aż do zweryfikowanego, czystego kodu gotowego do commita, poprzez dynamiczną orkiestrację wyspecjalizowanych skilli i subagentów.

> [!IMPORTANT]
> **Żelazna Zasada `/craftsman` (Zakaz Pomijania `/simplify`, `/code-review` i `/verify`):**
> Użytkownik wywołuje `/craftsman` właśnie po to, aby **nie musieć ręcznie uruchamiać** `/simplify`, `/code-review`, `/audit` oraz `/verify`.
> 1. **Zakaz przeglądu „w myślach” (inline) przez głównego agenta:** Niezależnie od tego, czy zrobiłeś 1 commit czy 9 małych commitów, oceniany jest **cały skumulowany diff od początku zadania (`base_sha..HEAD` + working tree)**.
> 2. **Obowiązkowy Niezależny Subagent (`Twórca ≠ Recenzent`):** Po zakończeniu implementacji (Etap 0) **MUSISZ** powołać niezależnego subagenta przez `invoke_subagent` (`TypeName: "self"`, `Role: "Craftsman Quality Gate Auditor"`), który ze świeżym oknem kontekstowym faktycznie odczyta pliki `SKILL.md` dla `/simplify`, `/code-review` i `/verify` oraz wykona je na całym skumulowanym diffie.
> 3. **Weryfikacja w `transcript.jsonl`:** Systemowy hook `Stop` (`craftsman-stop-gate.py`) skanuje `transcript.jsonl`. Jeśli spróbujesz zakończyć `/craftsman` (lub wywołać `--complete`) bez realnego uruchomienia `invoke_subagent` dla Bramki Jakości, hook fizycznie zablokuje zakończenie tury.

Komunikuj się **po polsku** z zachowaniem angielskich terminów technicznych i klikalnymi linkami `[plik.ext:L10-L20](file:///absolutna/sciezka/plik.ext#L10-L20)`.

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
[Etap 1: Niezależny Subagent Bramki Jakości (invoke_subagent: "Craftsman Quality Gate Auditor")]
   │  (Działa na całym skumulowanym diffie: git diff <base_sha>)
   ├── 1. Wczytanie i wykonanie /simplify     (Reuse, Simplification, Efficiency, Altitude -> edycja)
   ├── 2. Wczytanie i wykonanie /code-review  (Angles A–E: poprawność + konwencje projektu)
   ├── 3. Wczytanie i wykonanie /audit        (audit-errors, audit-types, audit-tests, audit-security)
   ├── 4. Auto-Fix                            (Bezpośrednia naprawa wszystkich znalezisk CONFIRMED/PLAUSIBLE)
   └── 5. Wczytanie i wykonanie /verify       (Testy jednostkowe + uruchomienie End-to-End w runtime)
          │
          ▼
[Etap 2: Zamknięcie craftsman_state.json (--complete) + Karta Odbioru & /high-level-commit-message]
```

---

## Etap 0 — Dynamiczny Router, Zapis `base_sha` i Implementacja

0. **Zainicjalizuj stan Hybrydowego Hard-Loopa i zapisz `base_sha`:**
   Na samym początku wywołania `/craftsman` uruchom w terminalu:
   ```bash
   ~/.gemini/config/scripts/craftsman-stop-gate.py \
     --state-file "<artifactDirectoryPath>/craftsman_state.json" \
     --init --cwd "<sciezka_do_repo>"
   ```
   Skrypt automatycznie zapisze bieżący commit `base_sha` (`git rev-parse HEAD`) oraz indeks kroku startowego w `transcript.jsonl`, aby na końcu rozliczyć **cały skumulowany diff** (wszystkie pośrednie commity + working tree).
   *(Jeśli w trakcie Etapu 0 musisz zatrzymać się i zadać pytanie użytkownikowi przez `ask_question`, wywołaj `--await-user` lub po prostu użyj narzędzia `ask_question` — hook `Stop` automatycznie rozpoznaje oczekiwanie na odpowiedź użytkownika).*

1. **Sprawdź kontekst startowy:**
   Uruchom `git status --short` oraz `git diff --stat` (względem `HEAD` lub `@{upstream}...HEAD`).
2. **Wybierz odpowiednią ścieżkę na podstawie argumentu `/craftsman [<zadanie>]`:**
   - **Ścieżka A — Nowa funkcjonalność (`Feature Track`):**
     Przeczytaj i zastosuj proces z [`/feature-dev`](file:///Users/rafalbuczynski/.gemini/config/skills/feature-dev/SKILL.md):
     1. Zbadaj istniejące wzorce w repozytorium (bezpośrednio lub przez równoległe subagenty `research`).
     2. Zadaj pytania wyjaśniające niejednoznaczności (`ask_question`) i zaczekaj na decyzję użytkownika.
     3. Zaproponuj 2–3 warianty architektoniczne z rekomendacją.
     4. Po akceptacji wdróż kod i testy, a następnie **obowiązkowo przejdź do Etapu 1 (Niezależny Subagent Bramki Jakości)**.
   - **Ścieżka B — Modernizacja lub głęboki refaktor (`Modernize Track`):**
     Przeczytaj i zastosuj [`/modernize`](file:///Users/rafalbuczynski/.gemini/config/skills/modernize/SKILL.md) (wyciągnij ukryte reguły domenowe do specyfikacji *Given/When/Then*, zabezpiecz je testami, wykonaj transformację), a następnie **przejdź do Etapu 1**.
   - **Ścieżka C — Masowa zmiana w wielu plikach (`Batch Track`):**
     Przeczytaj i zastosuj [`/batch`](file:///Users/rafalbuczynski/.gemini/config/skills/batch/SKILL.md) (rozbij na rozłączne *Work Units*, po akceptacji planu uruchom równoległe subagenty), a następnie **przejdź do Etapu 1**.
   - **Ścieżka D — Domknięcie bieżących zmian (`Polish & Ship Track`):**
     Gdy użytkownik wywołał samo `/craftsman` bez argumentów (lub poprosił o dopieszczenie/sprawdzenie bieżących zmian w repozytorium) → przejdź **od razu do Etapu 1 (Niezależny Subagent Bramki Jakości)** na aktualnym lub skumulowanym diffie.

---

## Etap 1 — Obowiązkowy Niezależny Subagent Bramki Jakości (`Twórca ≠ Recenzent`)

> [!CAUTION]
> **Zakaz wykonywania Etapu 1 „na skróty” w głównym wątku!**
> Nawet jeśli podczas pisania kodu uruchamiałeś testy i robiłeś małe commity, jako autor kodu ulegasz ślepocie autorskiej (*author bias*). **Musisz** zlecić Etap 1 niezależnemu subagentowi z czystym kontekstem przez narzędzie `invoke_subagent`.

Wywołaj `invoke_subagent` z następującymi parametrami:
- **`TypeName`**: `"self"` *(subagent musi mieć dostęp do `view_file`, `replace_file_content` i `run_command`, aby bezpośrednio aplikować poprawki `/simplify`, naprawiać błędy z `/code-review` i uruchamiać `/verify`)*
- **`Model`**: `"inherit"`
- **`Role`**: `"Craftsman Quality Gate Auditor"`
- **`Prompt`**: Przekaż subagentowi ścieżkę repozytorium, `base_sha` (odczytane z `craftsman_state.json` lub `git log`) oraz poniższą instrukcję wykonawczą:

```markdown
Jesteś niezależnym audytorem i egzekutorem Bramki Jakości Craftsman (Twórca ≠ Recenzent).
Pracujesz w katalogu: <CWD_REPO>
Twój zakres audytu to CAŁY SKUMULOWANY DIFF od początku zadania: `git diff <BASE_SHA>` (obejmujący wszystkie commity od <BASE_SHA> do HEAD oraz niezacommitowane zmiany).

Wykonaj bez żadnych dróg na skróty następujące 5 kroków:
1. Przeczytaj przez `view_file` pełne instrukcje:
   - `/Users/rafalbuczynski/.gemini/config/skills/simplify/SKILL.md`
   - `/Users/rafalbuczynski/.gemini/config/skills/code-review/SKILL.md`
   - `/Users/rafalbuczynski/.gemini/config/skills/verify/SKILL.md`
2. KROK 1 (/simplify): Przeanalizuj cały skumulowany diff (`git diff <BASE_SHA>`) pod kątem 4 kątów z `/simplify` (Reuse, Simplification, Efficiency, Altitude) i bezpośrednio zaaplikuj uproszczenia w plikach.
3. KROK 2 (/code-review + /audit): Prześwietl cały skumulowany diff pod kątem Angles A–E z `/code-review` oraz adekwatnych audytów (`/audit-errors`, `/audit-types`, `/audit-tests`, `/audit-security`). Wszystkie problemy o statusie CONFIRMED lub PLAUSIBLE napraw od razu w kodzie.
4. KROK 3 (/verify): Uruchom pełny zestaw testów oraz realną weryfikację End-to-End w runtime zgodnie z `/verify`. Jeśli cokolwiek nie przechodzi, napraw to do skutku.
5. Zwróć zwięzły raport z 3 sekcjami:
   - Co uproszczono w `/simplify` (ze wskazaniem plików i linii),
   - Jakie błędy/ryzyka wykryto i naprawiono w `/code-review` + `/audit`,
   - Jakimi komendami i scenariuszami potwierdzono `/verify` (wraz z wynikiem).
```

### Krok 1.6: Zamknięcie Bramki Hard-Loop (`craftsman_state.json`)
Dopiero **PO** otrzymaniu raportu od subagenta `Craftsman Quality Gate Auditor` (i ewentualnym zacommitowaniu jego poprawek czyszczących/naprawczych) uruchom:
```bash
~/.gemini/config/scripts/craftsman-stop-gate.py \
  --state-file "<artifactDirectoryPath>/craftsman_state.json" \
  --complete
```
*(Uwaga: skrypt `craftsman-stop-gate.py` weryfikuje w `transcript.jsonl`, czy `invoke_subagent` został faktycznie wywołany — próba wywołania `--complete` bez uprzedniego uruchomienia subagenta audytującego zostanie odrzucona kodem wyjścia `1`!).*

---

## Etap 2 — Karta Odbioru Rzemieślniczego i Propozycja Commita

Zakończ pracę przejrzystym podsumowaniem zawierającym wyniki od niezależnego subagenta:

```markdown
### 🛠️ Raport `/craftsman`
- **Ścieżka:** `<Feature | Modernize | Batch | Polish & Ship>`
- **Zakres skumulowanego diffu (`<base_sha>..HEAD`):** `<X plików, +Y/-Z linii>`
- **Audytor Bramki Jakości:** Niezależny subagent `Craftsman Quality Gate Auditor` (`conversation://<id>`)
- **Zmodyfikowane pliki:**
  - [plik.py](file:///pelna/sciezka/plik.py) — `<krótki opis roli zmiany>`

#### Przebieg Bramki Jakości (Niezależny Subagent):
1. **Simplify (`/simplify`):** `<co uproszczono / zoptymalizowano lub "kod był czysty">`
2. **Code Review & Audits (`/code-review` + `<uruchomione audyty>`):** `<wyłapane i naprawione błędy lub "0 problemów">`
3. **Verify (`/verify` w Runtime):** ✅ **VERIFIED** — `<jaką komendą/scenariuszem potwierdzono działanie>`

#### Proponowany Commit (`/high-level-commit-message`):
```text
<typ>(<zakres>): <zwięzły opis high-level w American English bez detali niskopoziomowych i bez AI attribution>
```
```
