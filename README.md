# Night Ledger

A Minervini VCP + momentum scanner for NSE/BSE that runs as **one always-on
web app**. Open the URL on a laptop, a tablet or a phone; the watchlist and the
alert tape are the same everywhere, and alerts push to every open browser the
instant they fire.

```
┌───────────────┬──────────────────────────────┬─────────────────┐
│ 01 WATCHLIST  │ 02 THE TAPE                  │ 03 SETUP        │
│               │                              │                 │
│ search + add  │ live alert feed (SSE)        │ contractions    │
│ live prices   │ VCP · breakout · momentum    │ pivot / stop    │
│ VCP badges    │                              │ trend template  │
└───────────────┴──────────────────────────────┴─────────────────┘
```

---

## What it actually checks

**Stage 2 trend template — runs first, and short-circuits everything else.**
A gorgeous chart pattern on a stock in a downtrend is a bull trap, so nothing
gets pattern-screened until it clears all eight gates: price above the 50 /
150 / 200 SMA, those averages stacked 50 > 150 > 200, the 200 SMA rising over
the last month, at least 25% off the 52-week low, within 25% of the 52-week
high, and outperforming the Nifty.

**Volatility Contraction Pattern.** Swing pivots are detected with a fractal
window, then walked left to right. The scanner keeps the longest recent run
where each pullback is at most 80% of the one before it (15% → 8% → 3%), and
requires the base to be at least 15 sessions old, the first leg no deeper than
35%, the final leg tighter than 12%, and volume in that final coil under 75%
of the 50-day average. The buy point is the high of the last contraction; the
stop is its low.

**15-minute momentum.** A rolling price tape per symbol. When the current
price is 2% above where it traded ~15 minutes ago, it fires — with a 30-minute
per-symbol cooldown so one runner does not scream at you all afternoon.

**Breakout.** Once a base is valid, any intraday print above the pivot fires
once per symbol per day.

Every threshold above is an environment variable. Nothing is hard-coded.

---

## Run it locally

```bash
git clone <your-repo> night-ledger && cd night-ledger
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional, it runs fine with defaults
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>, search `tata`, click `+`. The scanner picks the
symbol up on its next pass — no restart, that is the whole point of keeping
the watchlist in the store rather than in the source.

Tests (no pytest needed):

```bash
python -m unittest discover -s tests -t . -v
```

---

## Put it on the internet

You need a process that stays running. This is exactly the thing Vercel and
Netlify cannot do — their functions are event-driven and get torn down after a
response, so a `while True:` loop or a held-open broker WebSocket has nowhere
to live.

### Render (blueprint included)

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo. `render.yaml` sets up the
   web service, a 1 GB disk for the watchlist, and the health check.
3. Set `ACCESS_TOKEN` (the blueprint generates one) and open the site once as
   `https://your-app.onrender.com/?token=THE_TOKEN`. The token is swapped for
   a cookie, so the bookmark on your phone works from then on.

**On the plan:** `render.yaml` asks for `starter` (~$7/mo) because Render's
free web services spin down after ~15 minutes without inbound traffic — and a
sleeping scanner is not a scanner. If you want to try it free first, switch
`plan: free` and point a free uptime monitor (UptimeRobot, Better Stack) at
`/healthz` every 10 minutes to keep it awake. Watch the free-tier monthly hour
budget.

### Railway

New Project → Deploy from GitHub → it detects the `Dockerfile`. Add a volume
mounted at `/srv/data`, set the env vars from `.env.example`, done. Railway
bills by usage rather than sleeping the service.

### Any VPS (Oracle Cloud Always Free works well and is genuinely free)

```bash
sudo apt install -y python3-venv
git clone <your-repo> /opt/night-ledger && cd /opt/night-ledger
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo tee /etc/systemd/system/night-ledger.service >/dev/null <<'UNIT'
[Unit]
Description=Night Ledger scanner
After=network-online.target

[Service]
WorkingDirectory=/opt/night-ledger
EnvironmentFile=/opt/night-ledger/.env
ExecStart=/opt/night-ledger/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now night-ledger
```

`Restart=always` is the systemd equivalent of the PM2 pattern — if the network
drops or the process dies, it comes straight back. Put Caddy or nginx in front
for HTTPS, or expose it through a Cloudflare Tunnel to skip the firewall work.

> Run **one** instance, one worker. The scanner holds in-process state — the
> price tape, momentum buffers, the broker socket. Two workers means two
> scanners and every alert twice.

---

## Live ticks instead of delayed prices

Out of the box the intraday poller uses Yahoo, which lags the Indian exchange
by roughly 15 minutes. Daily bars are unaffected (a 15-minute lag on
yesterday's close means nothing), so **VCP detection is fully accurate on the
free feed** — it is only the momentum trigger that fires late.

For real ticks, get an Angel One SmartAPI account (free), create a *Market
Feeds* app at <https://smartapi.angelbroking.com>, and set:

```bash
FEED=angelone
ANGEL_API_KEY=...
ANGEL_CLIENT_CODE=...        # your login ID
ANGEL_PASSWORD=...           # the PIN, not the web password
ANGEL_TOTP_SECRET=...        # base32 string behind the 2FA QR code
```

The feed logs in, pulls the instrument master, maps your watchlist to
exchange tokens and holds a WebSocket open in a background thread with
exponential-backoff reconnects. If any credential is missing it logs a warning
and quietly falls back to Yahoo rather than starting up dead. The header pill
tells you which mode you are in at a glance.

Dhan slots in the same way — implement `LiveFeed` in `app/feeds/`, return
`live = True`, and add a branch in `build_live_feed`. That interface is four
methods.

---

## Phone push (optional)

The dashboard already streams alerts over SSE, which covers you whenever a tab
is open. For a locked phone, add either:

```bash
TELEGRAM_BOT_TOKEN=...   # from @BotFather
TELEGRAM_CHAT_ID=...     # your numeric chat id
DISCORD_WEBHOOK_URL=...  # channel settings -> integrations -> webhooks
```

Both are free and unmetered. Twilio SMS is deliberately not wired in — it
costs per message and buys you nothing over a push notification unless you are
genuinely offline.

---

## Layout

```
app/
  config.py        every threshold, env-overridable
  models.py        pydantic wire formats shared by scanner, API and browser
  strategy/        pure functions, no I/O — the unit-tested core
    indicators.py  SMA, ATR, fractal swing pivots, relative strength
    trend.py       Minervini trend template
    vcp.py         contraction walk, volume dry-up, pivot, quality score
    momentum.py    rolling %-move tape with gap and cooldown guards
  feeds/           HistoryProvider (daily bars) + LiveFeed (intraday)
  universe.py      NSE master with a checked-in seed-list fallback
  store.py         ACTIVE_WATCHLIST, alert tape, setup cache
  events.py        SSE fan-out
  scanner.py       the two loops
  main.py          FastAPI
  web/index.html   the dashboard, single file
tests/             37 tests, stdlib unittest only
tools/preview.py   offline UI preview against synthetic data
```

---

## Honest limitations

- **Yahoo intraday is delayed ~15 min for Indian equities.** The momentum
  alert is therefore ~15 minutes late until you switch to a broker feed. It is
  disclosed in the header rather than hidden.
- **Yahoo rate-limits.** Polling is batched (one request per 40 symbols) and
  daily bars are cached for an hour, but a 200-name watchlist on a 10-second
  poll will get you throttled. 30 seconds is a sane floor.
- **Corporate actions.** Daily bars are auto-adjusted, so a split mid-base
  will not fake a contraction — but a bonus issue on the exact day of a scan
  can still produce noise for a session.
- **Backtesting is not included.** The strategy module is deliberately pure
  functions over a DataFrame, so `analyse(df, symbol)` runs on any historical
  slice — the hook is there, the harness is not.
- **This finds setups. It does not size positions, place orders, or know
  anything about your risk tolerance.** A valid VCP is a reason to look at a
  chart, not a reason to buy.
