# Saarbrücken Mensa

A mobile menu reader with daily/weekly views, Korean/English/German, student/staff/guest prices, German originals and source notices.

[Public menu](https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/) · [Original source](https://www.stw-saarland.de/gastro/mensa-saarbruecken/)

This is an independent viewer. Menus and prices may change after collection.

## Local AI and durable refresh queue

A local Mac worker handles collection and translation. GitHub holds the validated JSON and serves the static site. Visitors never connect to the Mac. No hosted inference account or API key is required.

Every Monday at 09:17 Europe/Berlin a new weekly refresh becomes due. The local worker checks every 15 minutes and on login. If the computer was off, it reconstructs the most recent due request from the durable completion record; the request does not expire while offline. Multiple missed collection weeks become one request for the latest menu.

The lightweight check does not load an AI model. It waits while available memory is below 32 GiB, one-minute CPU load exceeds 60% of the logical CPU count (minimum threshold 2), or the Mac is on battery. It cannot measure every competing GPU workload; these are conservative readiness gates, not a general resource scheduler.

When ready, it:

1. Takes an exclusive process lock and synchronizes a dedicated clean checkout.
2. Fetches the source and applies price/coverage validation against the previous snapshot.
3. Reuses translations with unchanged source text; starts local Ollama only if new text needs inference.
4. Translates dish names and components with Gemma 4, checks both languages and component counts, then shuts down its own model process group.
5. Commits only the validated menu/translation JSON and pushes to GitHub.
6. Dispatches one Pages deployment pinned to that commit; acknowledges the queue item only after success.

Collection failures keep the previous public site and queued request. Retries use 15/30/60-minute backoff, capped at six hours. Publication checkpoints retain the commit and GitHub run ID across interruption. A failed deployment is retried as the same GitHub run. Ambiguous dispatch waits before trying again. An already successful publication is acknowledged after interruption. An unpublished snapshot that expires while the Mac is offline is replaced by a fresh collection before deployment.

GitHub Actions now only validates and deploys snapshots. It has a manual trigger and no separate weekly schedule or push trigger: the local queue owns the weekly refresh, avoiding duplicate jobs. Recovery or an explicitly requested manual update can create extra deployment attempts.

## Local installation and status

Python 3.12+, Git and authenticated GitHub CLI are required. The worker uses a dedicated `main` checkout at `~/Library/Application Support/Mensa/checkout`, a task-specific Ollama CLI at `runtime/ollama`, and model weights under `models/`. These paths are outside the public repository.

The configured local model is `gemma4:31b`. The initial smaller E2B trial made culinary errors and was not published. The 31B model and a small culinary glossary are used for the operational path. Existing editorial drafts are retained; the cache records which entries were actually model-generated. JSON validation does not prove semantic accuracy, so the German source remains visible.

After preparing the dedicated checkout, runtime and model, register the user LaunchAgent:

```sh
cd "$HOME/Library/Application Support/Mensa/checkout"
python3 -m scripts.install_local_worker
```

The installer registers `io.github.garlicvread.mensa-refresh` with `RunAtLoad` and a 900-second interval. It does not create a model service. The user must be logged into macOS for this user agent to run. The computer is never forcibly woken.

Inspect queue and recent results:

```sh
cd "$HOME/Library/Application Support/Mensa/checkout"
python3 -m scripts.local_refresh --status
cat "$HOME/Library/Application Support/Mensa/last-result.json"
```

Queue state is `queue.json`; the process lock is `worker.lock`; bounded model logs and worker stdout/stderr are in `logs/`. A pending request has its period, phase, retry time, last error and publication identifiers. Do not delete these files to force an update: they prevent lost work and duplicate publication.

A manual worker check uses the same resource and due-date rules:

```sh
python3 -m scripts.local_refresh
```

Disable the local worker without deleting its queue:

```sh
launchctl bootout "gui/$(id -u)/io.github.garlicvread.mensa-refresh"
```

An optional Codex follow-up checks queue failures and overdue publication and alerts in the existing task. The worker itself runs independently of Codex; Codex notifications require the app's automation to run.

## Price and translation integrity

Prices come only from the exact source meal identified by date, counter and German name. The raw S/M/G block is retained and compared with integer cents both in collection and in the browser. Raw source inventories are reconciled with extracted records. Missing previous prices, partial price groups, suspicious losses, invalid model JSON and incomplete translations block publication.

Future source entries without a price block provide a source-price link. No price is borrowed from another dish or guessed by the model. The source PDF does not contain prices and is not a fallback.

Ingredient, allergen and additive labels use a reviewed bilingual glossary in `data/notice-translations.json`. Every meal and side-dish notice is paired with its exact German original in Korean/English views. The weekly refresh checks notice coverage even when the dish translation is cached; an unknown label blocks publication and enters the existing retry/notification path until its glossary translation is reviewed.

The model receives dish names and components only, never prices or allergen notices. Native Ollama requests are loopback-only, disable thinking, constrain JSON with a schema and require normal completion. Unknown output shapes or truncated responses fail closed. The existing optional OpenAI-compatible provider remains available for manual use but is not used by the queued local worker.

Translated titles describe the food in the selected language. The `mensaVital` balanced-menu brand and `KlimaTeller` low-carbon label remain in the exact German subtitle, rather than being copied into Korean/English dish titles. A reviewed naming policy applies to cached, editorial and new model translations; validation rejects unresolved occurrences before publication. `Wikingertopf` is rendered as “Meatball stew” / “고기완자 스튜”, and `Köttbullar` as “Swedish meatballs” / “스웨덴식 미트볼”. These are culinary name translations, not additions to the source ingredient inventory. In particular, the viewer does not infer cream, vegetables or meat species from a generic recipe.

Naming references: [mensaVital definition](https://www.mensavital.de/mensavital/ueber-mensavital/die-marke-mensavital), [Saarland KlimaTeller explanation](https://www.stw-saarland.de/nachhaltig/), and [EDEKA’s Wikingertopf recipe](https://www.edeka.de/rezeptwelt/rezepte/wikingertopf/).

## Development and manual deployment

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_ui.mjs
python3 -m scripts.update_menu --validate-only
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

To deploy an already committed, validated snapshot explicitly:

```sh
gh workflow run update-and-deploy.yml --ref main -f snapshot_sha="$(git rev-parse HEAD)"
```

The workflow verifies that the requested commit belongs to `main`, runs tests and validation, and uploads only `site/`. See [data-contract.md](docs/data-contract.md) for schemas.
