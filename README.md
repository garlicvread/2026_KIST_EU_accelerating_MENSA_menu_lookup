# Saarbrücken Mensa

A mobile-first, unofficial menu reader for Mensa Saarbrücken. Browse every published day in daily or weekly views, switch between Korean, English and German, and select student, staff or guest prices. Original German names and source notices remain available.

Public site: https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/

Source: [Studierendenwerk Saarland](https://www.stw-saarland.de/gastro/mensa-saarbruecken/). This project is not operated by the Studierendenwerk. Menus and prices can change after collection.

## Run locally

Python 3.12+ and Node.js 22+ are sufficient; no package installation or frontend build is required.

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_ui.mjs
python3 -m scripts.update_menu --validate-only
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

Open http://127.0.0.1:8000/. To fetch a new menu, run `python3 -m scripts.update_menu` from the repository root.

## Weekly refresh and publication

`.github/workflows/update-and-deploy.yml` has one scheduled run: Monday at 09:17 Europe/Berlin. It also supports a manual **Run workflow** for setup, recovery and UI changes. There is deliberately no push trigger, so a saved menu does not start another workflow. A manual run with `refresh=false` deploys the already validated committed snapshot without fetching again.

Each run tests the collector and UI helpers, fetches the source, validates the new data against the previous snapshot, prepares translations, persists the successful snapshots to `main`, and publishes only `site/` to Pages. Configure the repository Pages source as **GitHub Actions**. A failed step stops publication and leaves the existing live deployment in place. GitHub provides the run's failure notification and step summary.

GitHub schedules may be delayed or skipped and public-repository schedules may be disabled after prolonged inactivity. The site shows the last successful collection and the dates covered; it does not pretend a failed or overdue refresh succeeded. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Price integrity

- Prices are extracted from the exact meal containing the matching date, counter and German title. S/M/G values become integer euro cents; the original price text is retained as provenance.
- Raw menu/price inventories are reconciled with the parsed records. Malformed or partial price groups, ambiguous identities and unexpected markup fail validation.
- A previously priced matching meal becoming unpriced fails validation. Suspicious losses on overlapping dates also block replacement.
- No price is inferred from another dish, date or category. Some future source entries currently do not contain a price block. Those entries provide a direct source-price link; this is not a statement that the cafeteria has no price.
- Both candidate files are validated before any existing snapshot is replaced. The translation cache is replaced before the menu and retains old keys, so it remains compatible with the prior snapshot. Publication happens only after the entire update succeeds.
- The source PDF does not contain prices and is not used as a price fallback.

## English and Korean translations

The initial cache is an editorial translation draft of the current German menu, not claimed to be Gemma output. It is stored in `site/data/translations.json`; the supporting phrase dictionary lives in `data/editorial-translations.json`. The interface exposes the German original and preserves source ingredient/allergen notices separately.

The cache key includes the original dish name and all component names. A changed name or side creates a new key. Model and prompt-version metadata are stored. Translation validation checks the response structure, source key, languages and component counts. It cannot prove culinary or linguistic correctness; inspect representative outputs when selecting a model.

With no model configured, weekly collection still works. Known translations are reused; new untranslated dishes display their German original with a clear label. Nothing calls a model from a visitor's phone, and no API secret is shipped to Pages.

### Connect Gemma later

Use an endpoint that implements OpenAI-compatible `POST /chat/completions`, accepts the `response_format: json_object` request field, and returns text JSON in `choices[0].message.content`. The model identifier must be the provider's actual Gemma model ID; a download name is not automatically a hosted API. No model service, account or billing is provisioned by this repository.

In repository **Settings → Secrets and variables → Actions**, set:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `MENU_TRANSLATION_URL` | Full HTTPS chat-completions endpoint URL |
| Variable | `MENU_TRANSLATION_MODEL` | Provider model ID |
| Secret | `MENU_TRANSLATION_API_KEY` | Provider API key, when required |

Then run the workflow manually with refresh enabled. Only uncached or model-version-invalidated entries require inference. Editorial cache entries are retained. Invalid JSON, incomplete translations, failed requests and non-successful generations reject the update; they do not overwrite the previous published data. Credentials must never be added to committed files.

For a local OpenAI-compatible server, a loopback HTTP endpoint is allowed for development. A GitHub-hosted runner cannot access a model running on your laptop's `localhost`; use a reachable HTTPS provider for the scheduled workflow. Provider-specific authentication and request formats may need an adapter. This integration is tested against a local protocol fixture, not a live Gemma service.

See [data-contract.md](docs/data-contract.md) for the source and cache schemas.
