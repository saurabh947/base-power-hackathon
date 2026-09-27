# Demo script (3 minutes)

**Site:** https://fleet-sim.vercel.app
**Backend:** Cloud Run `fleet-sim`, project `appintellect`, us-east1.

## Once, before demo day
1. Optional, for judging day only: keep the backend warm so nobody waits for it to wake up. This costs about $7/day; set it back to `0` afterwards.
   ```bash
   gcloud run services update fleet-sim --project appintellect --region us-east1 --min-instances 1
   ```

## Two minutes before
- Open the site; the first visit wakes the simulator.
- **Crash a controller** is open to everyone. It's on the Live screen's controls, or click any controller's tile. At most one crash every 2 s; **Reset** brings every controller back.
- On the Start screen the defaults read: 20,000 home batteries · Dallas–Fort Worth, Houston, San Antonio and Austin · **Biggest spike of 2025, Apr 7, 2025, from 3 am** · 1 hour every 6 s · AI forecast (TimesFM-3) · 90% charged.
- **Do one practice run first.** The race numbers for this morning weren't measured before this preset became the default, so read them off the Race panel once and use those in what you say.

## The 3 minutes
The replay runs at 1 hour of market time every 6 seconds, so the spike comes about 20 seconds in. If you want more time to talk, set the speed to *1 hour every 12 s* before you press Start.

| Time | Replay | Do | Say |
|---|---|---|---|
| 0:00 | 3 am | Point at the settings card, then press **Start the simulation** | "20,000 home batteries across the Texas Triangle replay April 7, 2025: the biggest price spike of the year. Watch the top right: our AI fleet races a copy of the same fleet running a simple rule." |
| ~0:08 | ~4:30 am | Press **Crash a controller** | "That really killed a process on the server. The tile goes red and its 1,000 dots flash. The other 19 take them over in milliseconds, and 'asked for' and 'delivered' line up again on the next step." |
| ~0:17 | 5:45 am | The feed reads "Prices jumped to $3xx in all four cities. The AI fleet had …% charge left; the simple rule had …%." | "Prices are climbing. This is who kept charge for what comes next." |
| ~0:23 | 6:45 am | All four cities pass $3,500/MWh. **Crash a second controller** | "The year's highest prices, two controllers down, and the fleet still delivers what it's asked for." |
| ~0:30 | 7:30 am | Point at **Race** | "Same batteries, same prices, only the decisions differ." Read the two totals from the screen. |
| optional | | Click **Houston** on the map | "Every city has its own price, money and race. These seven controllers run Houston." |
| 1:30 | | Open **Results** | "Over all of 2025 that's $191 per battery per year, $3.8M for this fleet." |
| 1:50 | | Point at **What the AI adds**, beside it | "+$67 per battery per year, +54% over the simple rule: $1.35M more for the fleet. The bars show it gets 45% of the way to perfect hindsight, and it soaks up 5.7× more free or negative-priced power." |

- **Too fast?** Use **Pause**, or set the speed to *1 hour every 12 s*.
- **Next run:** press **Reset**. That brings all 20 controllers back and every battery online.

## Honest answers to likely questions
- **"Does the AI always win?"** No.
  - On the evening of July 11, 2025 both fleets catch the 9 pm spike, and the AI ends only about $1,500 ahead.
  - On the evening of May 16, 2025 the AI sold too early.
  - Its best evening of 2025 was May 30: $38,936 against the simple rule's $17,536.
  - Over whole years it wins clearly: +54% in 2025 and +139% in 2024.
- **"Why April 7?"** It has the year's highest prices: all four cities pass $3,500/MWh just before 7 am. An ordinary day and the whole year from January 1 are one click away on Start.
- **"Is $191 cherry-picked?"** The forecast rule's two settings were tuned on part of 2025, so 2024, with fresh forecasts, is the clean check: $271 per battery vs $114 for the simple rule.
- **"Real hardware?"** Everything talks through four frozen interfaces (price feed, battery adapter, strategy, ledger), so real price feeds and battery APIs can plug in later.

## If something goes wrong
- **"Waking up the simulator…"** means the backend was idle; it takes a few seconds.
- **Anything looks stuck:** press **Reset**, then **Start**.
- **Backend logs:** `gcloud run services logs read fleet-sim --project appintellect --region us-east1 --limit 50`
- **Health check:** `curl https://fleet-sim-806389099.us-east1.run.app/health`
