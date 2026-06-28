# Kalshi Fee Dashboard

Static dashboard for estimated Kalshi fee revenue. The browser app reads `kalshi_fee_daily.csv` from this folder and renders the headline stats, filters, and charts.

## Run The Website

```bash
python3 -m http.server 8000
```

Open `http://localhost:8000`.

## Generate Data

Keep the Python collector in `scripts/`, not `src/`. The `src/` folder is only for browser JavaScript modules.

Run the collector from the project root so it writes CSVs beside `index.html`:

```bash
python3 scripts/kalshi_fee_calculator.py --output-dir .
```

Useful options:

```bash
python3 scripts/kalshi_fee_calculator.py --days 365 --output-dir .
python3 scripts/kalshi_fee_calculator.py --skip-perps --output-dir .
python3 scripts/kalshi_fee_calculator.py --taker-fraction 0.8 --output-dir .
```

The collector writes:

- `kalshi_fee_daily.csv`
- `kalshi_fee_monthly.csv`
