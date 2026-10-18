# Nova Gorica Open Data Dashboard (test version)

Test deployment of the open-data traffic dashboard developed within the **EnCLOD** project (Interreg Central Europe, CE0200649) by the University of Ljubljana, Faculty of Architecture, together with the Municipality of Nova Gorica.

The dashboard shows traffic counts from 26 Telraam S2 sensors in Nova Gorica: pedestrians, two-wheelers, cars, heavy vehicles and night-time traffic, for the last hour, day or week. Each page load highlights a different sensor. Full hourly data for every sensor can be downloaded as CSV.

## How it works

The page itself is static and is served by GitHub Pages. A scheduled GitHub Actions workflow runs every hour, requests the latest hourly counts from the Telraam API and publishes them next to the page as small JSON files (last 7 days) and CSV files (all available hours since 2024). The CSV history is extended incrementally on every run.

The Telraam API key is stored as a repository secret (`TELRAAM_API_KEY`) and never reaches the browser. Visitor numbers do not affect API usage, since the API is called only by the workflow.

```
index.html                    dashboard (single file)
scripts/fetch_telraam.py      hourly data fetch
.github/workflows/update.yml  schedule, build and Pages deployment
```

## Setup

1. Settings → Pages → Source: **GitHub Actions**.
2. Settings → Secrets and variables → Actions → New repository secret: `TELRAAM_API_KEY`.
3. Actions → *Posodobi podatke in objavi stran* → Run workflow.

Without the secret the page is still published and falls back to demo values.

## Notes

- GitHub pauses scheduled workflows in public repositories after 60 days without repository activity. Any commit, or re-enabling the workflow in the Actions tab, restarts it.
- Data source: Telraam S2 sensors, Nova Gorica network. Open data.

Contact: Tomaž Berčič, University of Ljubljana, Faculty of Architecture.
