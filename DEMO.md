# Demo script (3 minutes)

**Site:** https://fleet-sim.vercel.app
**Backend:** Cloud Run `fleet-sim`, project `appintellect`, us-east1.

## Once, before demo day
1. Set your presenter passcode. Only you can crash controllers. Run this in a terminal. The passcode is never shown, and never saved in your shell history:
   ```bash
   read -rs "PASS?Presenter passcode: " && printf '%s' "$PASS" | gcloud secrets versions add fleet-sim-presenter-passcode --project=appintellect --data-file=- && unset PASS
   ```
   The running backend reads the new passcode within a few minutes. To be sure, redeploy with `make cloud-deploy`.
2. Optional, for judging day only: keep the backend warm so nobody waits for it to wake up. This costs about $7/day; set it back to `0` afterwards.
   ```bash
   gcloud run services update fleet-sim --project appintellect --region us-east1 --min-instances 1
   ```

## Two minutes before
- Open the site; the first visit wakes the simulator.
- Click **Presenter mode** (top right, next to "Show technical details") and enter your passcode. The **Crash a controller** button appears on the Live screen.
- On the Start screen the defaults read: 20,000 home batteries · Dallas–Fort Worth, Houston, San Antonio and Austin · **The AI's best evening, May 30, 2025, from 6 pm** · 1 hour every 6 s · AI forecast (TimesFM-3) · 90% charged.

## The 3 minutes
The replay runs at 1 hour of market time every 6 seconds.

| Time | Replay | Do | Say |
|---|---|---|---|
| 0:00 | | Point at the settings card, then press **Start the simulation** | "20,000 home batteries across the Texas Triangle replay real prices from May 30, 2025. Watch the top right: our AI fleet races a copy of the same fleet running a simple rule." |
| ~0:05 | 6:30–7:30 pm | Point at **Race** | "The simple rule sells as soon as prices look high. It's ahead by about $12,000. The AI's TimesFM-3 forecast says prices keep climbing, so it waits at 90%." |
| ~0:12 | 8 pm | The AI starts selling. Press **Crash a controller** | "That really killed a process on the server. The tile goes red and its 1,000 dots flash. The other 19 take them over in milliseconds, and 'asked for' and 'delivered' line up again on the next step." |
| ~0:18 | 9 pm | The feed reads "Prices jumped to $389 in all four cities…" **Crash a second controller** | "Two down, still on target. The simple rule is almost empty; the AI kept energy for this." |
| ~0:25 | ~9:45 pm | The feed reads "The AI is now $21,330 ahead of the simple rule." | "Same batteries, same prices, only the decisions differ: $38,900 vs $17,500 tonight." |
| optional | | Click **Houston** on the map | "Every city has its own price, money and race. These seven controllers run Houston." |
| 1:30 | | Open **Results** | "Over all of 2025 that's $191 per battery per year, $3.8M for this fleet." |
| 1:50 | | Scroll to **What the AI adds** | "+$67 per battery per year, +54% over the simple rule: $1.35M more for the fleet. It gets 45% of the way to perfect hindsight, soaks up 5.7× more free or negative-priced power, and in 2024 it went into price spikes twice as charged." |
| 2:30 | | Flip the switch to **2024 (check year)** | "2024 is a second full year to check it holds: +$158 per battery, +139%." |

- **Too fast?** Use **Pause**, or set the speed to *1 hour every 12 s*.
- **Next run:** press **Reset**. That brings all 20 controllers back and every battery online.
- **Re-running the full year** on Results takes about a minute. **Stop** cancels it and keeps the current results.

## Honest answers to likely questions
- **"Does the AI always win?"** No.
  - On July 11 (the *Summer night spike* preset) both fleets catch the 9 pm spike, and the AI ends only about $1,500 ahead.
  - On May 16 (the *Price-spike evening* preset) the AI sold too early.
  - Over whole years it wins clearly: +54% in 2025 and +139% in 2024.
- **"Why this evening?"** The demo uses the 2025 evening where the AI pulled furthest ahead. The preset's label says so, and every preset is one click away.
- **"Is $191 cherry-picked?"** The forecast rule's two settings were tuned on part of 2025, so 2024, with fresh forecasts, is the clean check: $271 per battery vs $114 for the simple rule.
- **"Real hardware?"** Everything talks through four frozen interfaces (price feed, battery adapter, strategy, ledger), so real price feeds and battery APIs can plug in later.

## If something goes wrong
- **"Waking up the simulator…"** means the backend was idle; it takes a few seconds.
- **Anything looks stuck:** press **Reset**, then **Start**.
- **Backend logs:** `gcloud run services logs read fleet-sim --project appintellect --region us-east1 --limit 50`
- **Health check:** `curl https://fleet-sim-806389099.us-east1.run.app/health`
