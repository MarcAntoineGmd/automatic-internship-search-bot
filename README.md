# Canadian SWE Internship Radar

A personal Discord bot that checks ~110 job sources every 30 minutes and sends you a **private DM** when a new
Canadian software-engineering internship or co-op shows up. Québec/Ontario and Summer 2027 get the highest priority.
Filtering is deterministic (no LLM, no paid APIs). Every job is notified **at most once**, including after restarts.

---

## 1. Quick start

```bash
# Python 3.12+
python -m venv .venv
source .venv/bin/activate # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env # Windows: copy .env.example .env   -> then fill in the Discord values (section 2)

python -m bot scan --temp-db # optional: check the sources without Discord, using a throwaway DB
python -m bot # run the bot (keeps running; scans at startup, then every 30 minutes)
```

Other commands:

| Command | What it does |
|---|---|
| `python -m bot` | Run the Discord bot (needs `DISCORD_BOT_TOKEN`) |
| `python -m bot scan` | One scan without Discord, using your real `jobs.db` (no DMs are sent) |
| `python -m bot scan --temp-db` | Same, with a temporary DB. A pure smoke test that shows ✓/✗ per source |
| `python -m bot scan --temp-db --only greenhouse,lever` | Only some source types or ids |
| `python -m bot sources` | List configured sources |

---

## 2. Discord setup (about 5 minutes)

Bots can't start a DM with a stranger, so you add the bot to a small server you own and run `/subscribe` there once.

1. **Create the application.** Go to <https://discord.com/developers/applications> → **New Application** and give it a name (e.g. *Internship Radar*).
2. **Get the token.** Open the **Bot** tab → **Reset Token** → copy it into `.env` as `DISCORD_BOT_TOKEN=`.
   No privileged intents are needed, so leave *Message Content*, *Server Members* and *Presence* off.
3. **Invite the bot.** Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: `Send Messages` and `Embed Links`

   Open the generated URL and add the bot to a server you own. You can create a private server just for this (**+** in Discord → *Create My Own*).
4. **Copy the IDs.** In Discord go to **User Settings → Advanced → Developer Mode = on**. Then:
   - Right-click your server → **Copy Server ID** and paste it as `DISCORD_GUILD_ID=`. This makes slash commands appear right away.
   - Right-click your own name → **Copy User ID** and paste it as `DISCORD_USER_ID=`. Only this account can subscribe or run admin commands.
5. **Allow DMs.** Right-click the server → **Privacy Settings** → turn on **Direct Messages** from server members.
6. **Start the bot** with `python -m bot`. In the server, run **`/subscribe`**, then **`/test_notification`**. You should get a DM.

### Commands

| Command | Who | Description |
|---|---|---|
| `/subscribe` | owner | Register for DMs. Any matches already waiting are sent right away. |
| `/unsubscribe` | owner | Stop DMs. Scans keep running. |
| `/notify_here` | owner | Post new internships in the current server channel instead of DMs |
| `/notify_dm` | owner | Go back to DMs |
| `/status` | owner | Subscription, jobs stored, DMs sent, pending, active sources, last successful scan, next scan, source errors |
| `/scan` | owner | Run a scan now. It is refused if one is already running. |
| `/latest [count]` | owner | Most recently discovered matches |
| `/test_notification` | owner | Send a test message wherever notifications go (channel or DM) |
| `/applied <job_id> [status]` | owner | Track an application: `applied` (default), `interviewing`, `offer`, `rejected`, `ignored`, `new` |
| `/help` | anyone | Command list |

### Posting in a server channel instead of DMs
Run **`/notify_here`** in the channel you want, for example `#internships`. The bot checks that it has *View Channel*, *Send Messages* and *Embed Links* there.
From then on every new match is posted in that channel, and DMs stop. **`/notify_dm`** switches back.
You can also set `NOTIFY_CHANNEL_ID` in `.env` (right-click the channel → **Copy Channel ID**). If you've used `/notify_here` or `/notify_dm`, the command's choice wins over `.env`.
Jobs already notified are never re-posted when you switch.

### Manual DM verification (real Discord)
1. Fill in `.env` and run `python -m bot`. The log should show `Logged in as …` and `Synced 8 slash commands to guild …`.
2. Run `/subscribe` in your server and check that the reply says *Subscribed*.
3. Run `/test_notification` and check that the *Test notification* embed arrives in your DMs.
   If it says Discord refused the DM, redo step 5 of the setup.
4. Run `/scan`. On the very first scan you get one "Initial import" message plus the top 10 matches (see *Initial import* below).
5. Run `/scan` again. You should get **no new DMs**, which shows that duplicate suppression works.
6. Stop the bot (Ctrl+C), start it again and wait for the startup scan. Again you should get no repeated DMs.
7. Run `/status` and check the counts and any source errors.

---

## 3. What gets notified

### Sources (109 configured in `sources.toml`)

| Type | Count | How |
|---|---|---|
| Aggregators | 3 | SimplifyJobs *Summer2027-Internships* and vanshb03 *Summer2027-Internships* (`listings.json`), negarprh *Canadian-Tech-Internships-2027* (README table). Fetched with ETag caching, so an unchanged file costs a 304. |
| Greenhouse | 37 | `boards-api.greenhouse.io/v1/boards/<board>/jobs`: Lyft, Stripe, Pinterest, Databricks, Tower Research (Montréal), Hootsuite, Epic Games (Montréal), Faire, Geotab, Tenstorrent, Robinhood, Toast, Okta, D2L, MongoDB, AppDirect, Workleap, Poka, ShareGate, … |
| Lever | 7 | `api.lever.co/v0/postings/<company>`: Waabi, Kepler, PointClickCare, Palantir, Wattpad, Spotify, Behaviour Interactive |
| Ashby | 29 | `api.ashbyhq.com/posting-api/job-board/<org>`: Wealthsimple, Cohere, 1Password, KOHO, Neo, Jane, Jobber, Benevity, Hopper, Trulioo, Snowflake, TELUS Digital, … |
| Workday | 30 | the public career-site JSON (`/wday/cxs/<tenant>/<site>/jobs`): RBC (×2), TD, BMO (×2), CIBC, Desjardins, Intact, Autodesk, NVIDIA, Thomson Reuters, Ciena, Manulife, Sun Life, Morgan Stanley, Warner Bros. Games, Cisco, CAE, BlackBerry, Interac, SOTI, Altera, Clio, OMERS, PSP, CDPQ, Canadian Tire, Salesforce, GM, Thales |
| SmartRecruiters | 3 | `api.smartrecruiters.com/v1/companies/<id>/postings?country=ca`: Ubisoft, ServiceNow, Intelerad |

**Companies without a supported public ATS API.** These are only covered when one of the aggregators lists them, and the aggregators do list most of them: Shopify, Google, Microsoft, Amazon, AMD, IBM, National Bank (Avature), Nokia (Oracle Cloud), Qualcomm (Eightfold), Kinaxis (iCIMS), SAP, Ericsson, Bell, TELUS (corporate), CGI, Scotiabank, Coveo, Genetec (Workable).
To add one, write an adapter (section 6).
**Not used: LinkedIn and Indeed.** They have no authorized public API for this, and scraping them would break their terms.
**CSJobs.ca:** no public feed or API was found.

To add a company, find its board id in its job URL (`job-boards.greenhouse.io/<board>/…`, `jobs.lever.co/<id>/…`,
`jobs.ashbyhq.com/<org>/…`, `<tenant>.wdN.myworkdayjobs.com/<site>/…`) and add a `[[greenhouse]]` (or `[[lever]]`, `[[ashby]]`, `[[workday]]`) table to `sources.toml`.
Broken ids show up under *Source errors* in `/status`.

### Filtering and scoring (deterministic)
Each posting gets a 0–100 score from these parts:

| Part | Points |
|---|---|
| Role from the title | Software/backend/frontend/full-stack/mobile/game programmer 45. Platform/infra/cloud/DevOps/SRE/embedded, ML/AI engineer, data engineer, security engineer 40. QA automation and quant dev 32. Data science/ML 28. Research 24. Vague "Engineering intern" 8. |
| Description fallback | An unusual title still scores 24–30 when the description clearly describes software work (≥4 signals such as Python, React, REST APIs, Git, Docker). |
| Internship evidence | In the title (intern, co-op, stage/stagiaire, student, PEY) 20. Employment type 18. Listed by an internship tracker 15. Description only 10. |
| Location | QC/ON 20, plus 3 for Montréal, Québec City, Toronto, Waterloo or Ottawa. Remote in Canada 15. Other provinces 12. Remote North America or worldwide 6. Unknown −12, but the posting is kept. |
| Term | Summer 2027 12. Fall/Winter 2027 8. Other future terms 5. Unknown 3. Already started −15. |
| Bonus and penalties | +4 if it says it's open to enrolled students. −15 for PhD-only roles, −8 for master's-only roles. |

**Always excluded:** senior or experienced roles, full-time roles that aren't internships, new-grad roles (unless `INCLUDE_NEW_GRAD=true`), sales/marketing/HR/finance/legal, IT and technical support, product and design, hardware/analog/ASIC, data and business analyst roles (re-enable with `INCLUDED_CATEGORIES`), unpaid roles (unless `ALLOW_UNPAID=true`), and postings that are only outside Canada (including "Remote – US").
**Kept with a lower score:** anything uncertain, such as an unknown location, vague titles, or past terms.
**Not DM'd:** jobs scoring 40–54 are stored and visible in `/latest` and the daily summary, but don't trigger a DM (`NOTIFY_MIN_SCORE=55`).
Unknown fields stay unknown. If a source gives no posting date, the DM says *"Posted: Not provided by source"* and only shows the discovery date.

### Duplicate handling
- Each job is stored under several keys: its source uid, a canonical ATS key read from the URL (`greenhouse:<id>` — including `?gh_jid=` on company sites —, `lever:<uuid>`, `ashby:<uuid>`, `workday:<tenant>:<req-id>`, `smartrecruiters:<id>`) and the cleaned-up URL. A fallback fingerprint uses company and title, with aliases (RBC = Royal Bank of Canada) and term/city words removed, and must also match on term and province.
- Direct ATS sources are processed before aggregators, so the stored record is the one with the richer data. Later sightings only update `last_seen` and `also_seen_on`.
- Keys are never deleted, so a posting that disappears and comes back is **not** treated as new.
- Rejected postings are cached so they aren't re-fetched or re-scored. The cache resets automatically when you change filter settings.
- `notify_status` is saved before the next DM goes out. A failed DM stays pending and is retried on the next scan. There is a cap of `MAX_DMS_PER_SCAN` per scan.
- **Initial import:** the first time a source is checked successfully, only its best `BASELINE_NOTIFY_LIMIT` (10) matches are DM'd. The others (about 400 on the first run) are stored, summarised in one message and browsable with `/latest`.

---

## 4. Configuration (`.env`)
All settings are listed in `.env.example`. The main ones:

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` / `DISCORD_USER_ID` | – | See section 2 |
| `NOTIFY_CHANNEL_ID` | – | Optional. Post in this channel instead of DMs (or use `/notify_here`). |
| `SCAN_INTERVAL_MINUTES` | 30 | Minutes between scans (minimum 5) |
| `DATABASE_URL` | `sqlite:///./jobs.db` | SQLite file. Use `sqlite:////data/jobs.db` in Docker. |
| `TARGET_PROVINCES` / `TARGET_CITIES` / `TARGET_TERMS` | QC,ON / … / Summer 2027,Fall 2027,Winter 2027 | Priorities, in order |
| `INCLUDED_CATEGORIES` | software…general | Role categories to keep |
| `EXCLUDED_KEYWORDS` | unpaid,volunteer,bénévole | Title keywords that always exclude a posting |
| `MIN_RELEVANCE_SCORE` / `NOTIFY_MIN_SCORE` | 40 / 55 | Minimum score to store / minimum score to DM |
| `INCLUDE_NEW_GRAD`, `ALLOW_UNPAID`, `INCLUDE_UNKNOWN_LOCATION` | false, false, true | Optional categories |
| `MAX_DMS_PER_SCAN`, `BASELINE_NOTIFY_LIMIT` | 25, 10 | Protection against floods of DMs |
| `DAILY_SUMMARY_ENABLED`, `DAILY_SUMMARY_HOUR`, `TIMEZONE` | false, 9, America/Toronto | Optional daily digest |
| `SOURCES_FILE` | sources.toml | Source list |

The real `.env` is in `.gitignore`. Never commit it.

---

## 5. Testing

```bash
pip install -r requirements-dev.txt
pytest # 161 unit and integration tests: mocked HTTP, temporary SQLite, no Discord calls
pytest -m live -rs # read-only smoke tests against real public APIs (skips hosts your network can't reach)
python -m bot scan --temp-db # full live scan of every configured source, with ✓/✗ per source
```

The tests cover normalization, Greenhouse/Lever/Ashby/Workday/SmartRecruiters/GitHub/Markdown parsing,
internship keywords (English and French), SWE relevance, Québec/Ontario priority, the "CA" (California vs Canada) trap,
remote eligibility, terms, duplicate detection across sources, persistence across restarts, notification de-duplication,
failed-DM retries, timeouts and retries (including 429 Retry-After), isolation of failing sources, overlapping-scan prevention,
scheduling, embed formatting and Discord limits, subscription and owner rules, and an end-to-end scan run twice plus after a "restart".

---

## 6. Deployment (keep the SQLite file!)
The database is what prevents repeated DMs, so the host **must keep the `jobs.db` file between restarts**.

- **Your own PC** (free). Run `python -m bot` while the computer is on. On Windows you can auto-start it with Task Scheduler ("At log on" → `…\.venv\Scripts\python.exe -m bot`, *Start in* = the project folder).
- **Small Linux VPS or Raspberry Pi** (recommended for 24/7). Copy the project to `/opt/internship-radar`, create the venv, then use `deploy/internship-radar.service` with systemd. Any free-tier VM with a normal disk works, for example the Oracle Cloud Always Free tier.
- **Docker.** Run `docker compose up -d --build`. The DB lives in the named volume `radar-data` (`/data/jobs.db`), so rebuilding or restarting keeps it. Back it up with
  `docker compose cp internship-radar:/data/jobs.db ./jobs-backup.db`.
- **PaaS** (Railway, Fly.io, Render…). Only use one with a **persistent volume** mounted at `/data`. Ephemeral containers, free web dynos and GitHub Actions cron lose the DB on every restart and would re-send old jobs. They are not suitable unless you add a persistent volume.

### Adding a new ATS
Create `bot/sources/<name>.py` with a `Source` subclass. Set `kind`, implement `async fetch(http, ctx) -> list[Job]` and raise
`SourceFetchError` on failure. Then register it in `bot/sources/__init__.py` and add `[[<name>]]` tables to `sources.toml`.

---

## 7. Known limitations
- Workday board ids (except RBC, which was checked live) come from real application URLs in the trackers. Workday
  sometimes renames sites. A renamed site shows as a 4xx error in `/status`.
- Workday's list endpoint only returns relative dates ("Posted 30+ Days Ago"). The exact date is read from the job detail, and older ones stay *unknown*.
- vanshb03 links are `go.vansh.nyc` short links. The bot resolves them so it can de-duplicate. If that fails, it falls back to company+title matching.
- Postings with no location at all are kept with a lower score instead of being dropped, so expect the occasional US-only role marked *Location unknown*.
- The keyword rules are tuned for English and French titles. Very unusual titles with no description can still be missed or mis-scored. Check `/latest` from time to time.
