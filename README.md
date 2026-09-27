# Fleet Sim

20,000 simulated home batteries trading on real ERCOT prices across the Texas triangle (Houston, Dallas-Fort Worth, San Antonio, Austin). Built for the Base Power Hackathon, Sep 25 - 27, 2026.

Live: https://fleet-sim.vercel.app

## What it does

- Replays real ERCOT real-time prices for 2025, and for 2026 through Sep 19 with a TimesFM-3 forecast after that.
- A coordinator sets the fleet's power target every 5 minutes. 20 controller processes, 1,000 batteries each, carry it out. Kill one and the others take over its batteries.
- An AI strategy built on Google's TimesFM-3 forecasts races a simple buy-low, sell-high rule on the same batteries and prices.
- Full-year results: in 2025 the AI strategy earns $191 per battery vs $124 for the simple rule (+54%).

## Stack

Python, FastAPI, Redis and Postgres on Google Cloud Run and Cloud SQL. React and Vite on Vercel.

## Layout

- `src/` backend: simulator, strategies, backtest, forecasts, API
- `web/` website
- `tests/` backend tests
- `docs/` API contract and UX notes
- `spec/` the original hackathon spec
- `assets/` screenshots and recordings
- `RUNLOG.md` decisions and measured numbers

## Run locally

```sh
make setup        # Python 3.12 and dependencies (needs uv)
make test         # backend tests
make web-install  # website dependencies (Node 22.12+)
make web-dev      # website on localhost:5173 with a built-in practice API
```

## Note

The AI forecast uses TimesFM-3 research weights, which are licensed for non-commercial use only.
