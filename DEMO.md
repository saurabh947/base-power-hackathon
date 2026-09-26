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
- Click **Presenter** in the footer and enter your passcode. The **Crash a controller** button appears.
- On the Start screen the defaults read: 20,000 home batteries · Houston & Dallas–Fort Worth · **Summer night spike, Jul 11, 2025, from 6 pm** · 1 hour every 6 s · AI forecast (TimesFM-3) · 90% charged.

## The 3 minutes
| Time | Do | Say |
|---|---|---|
| 0:00 | Point at the settings card, then press **Start the simulation** | "20,000 home batteries replay real Texas prices from July 11, 2025." |
| 0:10 | Point at the price chart and its purple band | "Real ERCOT prices. The band is Google's TimesFM-3 forecast. The fleet waits and tops up while power is cheap." |
| ~0:20 (8 pm in the replay) | The fleet starts **selling at 165 MW**. Press **Crash a controller** | "That really killed a process on the server. Watch the tile go red. Its 1,000 batteries go to the other 19 in about 6 ms, and 'asked for' and 'delivered' line up again on the next step." |
| ~0:30 | **Crash a second controller** | "Two down, and the fleet is still on target." |
| ~0:35 (9 pm) | The price jumps to **$1,755/MWh** | "The AI kept energy for this. The fleet is still selling, and the money counter jumps." |
| optional | **Take batteries offline**: 30% of Houston | "Homes go offline, and the plan shrinks to what's available. Still on target." |
| 2:00 | Open **Results** | "$183 per battery per year, $3.7M for this fleet. The spec's simple rule makes $120. Perfect hindsight would be $406, so this is 45% of the ceiling." |
| 2:40 | Open **How we got this number** | "Gross wholesale value. It excludes retail and grid charges and battery wear, and uses a 15-minute delay on prices, because you can't trade on a price before it's published." |

- **Too fast?** Use **Pause**, or set the speed to *1 hour every 12 s*.
- **Next run:** press **Reset**. That brings all 20 controllers back and every battery online.

## Honest answers to likely questions
- **"Does the AI always win?"** No. On May 16, 2025 (the *Price-spike evening* preset) it sold at 3–4 pm and missed the 7:45 pm spike. Forecasts rarely see spikes coming. Over the whole year it still earns 53% more than the simple rule.
- **"Is $183 cherry-picked?"** The forecast rule's two settings were tuned on part of 2025, so we ran 2024 as a clean test, with fresh TimesFM-3 forecasts for 2024.
  - The AI earned **$265 per battery**, against $113 for the simple rule and $143 for evening peak. That is 51% of the $516 hindsight ceiling.
  - 2024 had bigger spikes than 2025, so it is the stronger year, not a tuned one.
- **"Real hardware?"** Everything talks through four frozen interfaces (price feed, battery adapter, strategy, ledger), so real price feeds and battery APIs can plug in later.

## If something goes wrong
- **"Waking up the simulator…"** means the backend was idle; it takes a few seconds.
- **Anything looks stuck:** press **Reset**, then **Start**.
- **Backend logs:** `gcloud run services logs read fleet-sim --project appintellect --region us-east1 --limit 50`
- **Health check:** `curl https://fleet-sim-806389099.us-east1.run.app/health`
