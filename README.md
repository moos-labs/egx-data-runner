# egx-data-runner

Tests EGX-native data libraries (**egxpy** / **Borsa**) as an alternative to
Yahoo for Egyptian Exchange data, using the COMMANDS-variable GitHub Actions
pattern (edit the `COMMANDS` repository variable, dispatch the workflow
manually, read results from the `results` artifact).

First test: can either library deliver POUL OHLC **history** — specifically the
ex-dividend-day **open** prices, which Yahoo reports incorrectly (Yahoo's
ex-day open always equals the prior close)?

- `scripts/egx_fetch.py` — tries each source tolerantly, saves
  `results/<source>_poul.csv` on success, and always writes
  `results/verdict.json` + `results/report.md`.
- `requirements.txt` — core deps (requests, pandas).
- `requirements-egx.txt` — the EGX libraries themselves, installed in a
  tolerant workflow step so a broken library cannot kill the run.
