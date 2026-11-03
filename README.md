# compbot 🏆

A self-hosted competition assistant for TrueNAS SCALE (or any Docker host). Every day it:

![compbot dashboard](docs/dashboard.png)

1. **Discovers** new competitions posted in MoneySavingExpert's [Competitions Time](https://forums.moneysavingexpert.com/categories/competitions-time) forum (via the forum's public API).
2. **Triages** each one: web form, email entry, Instagram/Facebook/X, Gleam, app/account-only, purchase-required, magazine puzzles. It pulls out the closing date, entry link(s), the answer posted by other members, and "daily" / multi-site (MD) flags.
3. **Enters** the ones it can, using your saved details: it fills web forms in a headless Chromium (Playwright) and sends email entries from your competition Gmail. It takes screenshots before and after every submission.
4. **Checks your competition Gmail** (Inbox and Spam) for wins among the noise. It scores each email, matches it to a competition you entered, clicks "confirm your entry" links, flags likely scams, and alerts you on a possible win.
5. **Logs everything** in a SQLite database with a web GUI covering the dashboard, every competition and attempt, a manual queue, an inbox triage view, wins and an activity log.

## What it deliberately won't do

* **Solve CAPTCHAs or dodge bot detection.** If a form has reCAPTCHA, hCaptcha or Turnstile, or the site blocks automation, the comp goes to your **manual queue** with a screenshot.
* **Ignore the rules.** Many UK T&Cs say *"entries made using a script, macro or automated means will be void"*. compbot reads the page and its T&Cs and, by default, sends those comps to the manual queue with the exact clause quoted. An automated entry there could cost you the prize.
* **Use your social media accounts.** Instagram/Facebook/X comps are skipped, or queued for you if you prefer.
* **Spend money.** Purchase-required comps are skipped.

Expect roughly a third to a half of web-form comps to be enterable automatically. The rest land in the manual queue with everything you need: the link, the answer and click-to-copy details.

## Install on TrueNAS SCALE (24.10 Electric Eel or later)

### 1. Prepare a Gmail account for competitions

1. Turn on **2-Step Verification** for the Google account.
2. Create an **App password** at <https://myaccount.google.com/apppasswords>, named e.g. "compbot".
3. IMAP is on by default for Gmail. compbot only reads mail (it never deletes or marks it as read).

### 2. Create a dataset

In **Datasets**, create e.g. `tank/apps/compbot` (the "Apps" preset is fine). It must be writable by the **apps** user (UID/GID 568), which is the user the container runs as. The database and screenshots live here, so include it in your snapshots and backups.

### 3. Get the image onto the NAS

**Option A: GitHub Container Registry (default in `truenas-app.yaml`)**

Every push to `main` runs `.github/workflows/docker.yml` on GitHub's free hosted runners (no self-hosted runner needed). It runs the tests, then publishes `ghcr.io/rk110038/compbot:latest`. Because the repo is public, the package is public too, so TrueNAS can pull it without credentials. (If you ever make the repo private, either make the package public under **github.com/rk110038 → Packages → compbot → Package settings**, or add a GHCR credential in TrueNAS.) To update, push, wait for the Action to go green, then stop and start the app in TrueNAS so it re-pulls the image. The Ollama service (`compbot:ollama-latest`, rebuilt daily from the latest Ollama by `.github/workflows/ollama-image.yml`) also has `pull_policy: always`, so the same stop/start picks up Ollama fixes too (new models such as Gemma 4 often need a recent Ollama). If you installed from an older copy of the YAML, add that line to the `ollama` service.

**Option B: build on the NAS**

Copy this folder to the NAS (for example into `/mnt/tank/apps/compbot-src` over SMB), then in **System → Shell**:

```bash
sudo docker build -t compbot:latest /mnt/tank/apps/compbot-src
```

Then set `image: compbot:latest` and `pull_policy: never` in the YAML.

### Option: Docker Compose from the TrueNAS shell (includes the AI)

Instead of Install-via-YAML you can run the stack with Compose from **System → Shell**. This is the easiest way to get the bundled **Ollama** AI with GPU acceleration:

```bash
mkdir -p /mnt/appspool/compbot && cd /mnt/appspool/compbot
for f in docker-compose.yml docker-compose.gpu-nvidia.yml docker-compose.gpu-amd.yml docker-compose.gpu-vulkan.yml docker-compose.searxng.yml; do
  sudo curl -fsSLO https://raw.githubusercontent.com/rk110038/compbot/main/$f; done
[ -f .env ] || sudo curl -fsSLo .env https://raw.githubusercontent.com/rk110038/compbot/main/.env.example
sudo nano .env        # passwords, Gmail, COMPBOT_UID/GID, and COMPOSE_FILE for your GPU (see below)
sudo mkdir -p data ollama && sudo chown -R "$(grep ^COMPBOT_UID .env | cut -d= -f2):$(grep ^COMPBOT_GID .env | cut -d= -f2)" data
sudo docker compose up -d
```

Update later with `sudo docker compose pull && sudo docker compose up -d` (this updates Ollama as well as compbot).

### 4. Install the app

**Apps → Discover Apps → ⋮ → Install via YAML**. Paste [`truenas-app.yaml`](truenas-app.yaml), change every `CHANGE_ME` (dataset path, GUI password, Gmail address and app password), and save.

### 5. First run

1. Open `http://<truenas-ip>:8085` and sign in.
2. **Settings**: fill in your details (name, email, address, date of birth, phone). Leave **Entry mode = Dry run** for now.
3. **Dashboard → Discover → Run now.** The first run imports threads from the last 14 days (configurable).
4. **Dashboard → Enter competitions → Run now.** In dry-run mode it fills each form, screenshots it and *doesn't* submit. Open a few competitions and check the screenshots and the field-by-field table.
5. Happy? **Settings → Entry mode = Auto.** From then on it runs on schedule.

Default schedule (Europe/London): discover 07:00, enter 07:30, inbox 08:00 and 20:00, maintenance 03:00. Change these with the `*_CRON` variables.

## Using it

| Page | What it's for |
|---|---|
| **Dashboard** | Totals (entered, won, lost, win rate), entries per day, jobs with **Run now**, comps closing soon, recent activity |
| **Competitions** | Every thread found, filterable by status, method and outcome, with CSV export. Each comp has a detail page with the original post, every attempt with screenshots, related emails and buttons to enter, dry-run, mark won/lost, skip, edit the answer or link |
| **Manual queue** | Comps that need you. Click-to-copy answers and personal details, **Open ↗**, then **I've entered it**. Marking manual entries matters, because it lets the inbox checker match a win email to them |
| **Inbox** | Classified emails (possible win, loss, confirm, entry acknowledgement, scam suspect, promo). Confirm or reject wins and link emails to competitions |
| **Wins** | Confirmed and possible wins with total stated prize value |
| **Live** | Watch the browser compbot is driving as it happens: a live picture of the page plus the current competition, step and result. Watch-only, and streamed from memory, so there are no disk writes. Switch it off in Settings → Live view |
| **Log** | Everything compbot and you did, plus job run history |

The 🌓 button at the top right switches the GUI between **Auto** (follows your device's light/dark setting), **Light** and **Dark**. The choice is remembered in that browser.

Quizzes that unlock the entry form (answer tiles or buttons, "Reveal question", "I've read the rules" boxes, "Enter the prize draw" links) are played automatically: the MSE post's answer first, then the AI, then a guess, and the next option after a "wrong, try again". Switch this off in Settings → Play quizzes.

Statuses: `queued` (will be auto-entered) → `entered` (or `manual`, `skipped`, `expired`). A comp whose first try fails stays `queued` for one more try on the next run (the dashboard's **Queued** card and the run summary say how many are waiting to retry); a multi-site comp entered on only some sites stays `queued` for the rest. Each entry run first moves out anything it could never enter: closed comps expire, and comps with no entry link, a method compbot can't enter, or email entries switched off go to the manual queue with the reason. Anything not yet entered expires once its closing date passes (checked at 03:00, after each discovery run and at the start of each entry run); a manual-queue comp with no closing date in its title expires 30 days after it was found. Outcomes: `pending` → `possible_win` → `won` / `lost`. Entered comps are marked **lost** automatically 60 days after closing if no win email arrived (most promoters only contact winners).

**Daily comps** (tagged "Daily") are re-entered every day until they close. **(MD) threads** list the same prize on several magazine sites, and compbot enters each one (configurable).

## Configuration

Secrets and infrastructure go in environment variables (in the YAML). Everything else is under **Settings** in the GUI.

| Variable | Default | Notes |
|---|---|---|
| `WEB_USER` / `WEB_PASSWORD` | `admin` / — | HTTP Basic auth for the GUI. **Set a password.** |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | — | Competition Gmail. Used for IMAP (reading) and SMTP (email entries, notifications) |
| `IMAP_FOLDERS` | `INBOX,[Gmail]/Spam` | Winner emails often land in Spam |
| `NOTIFY_EMAIL` | — | Where to send "possible win" alerts |
| `NTFY_URL` | — | e.g. `https://ntfy.sh/<secret-topic>` for phone push alerts |
| `DISCOVER_CRON`, `ENTER_CRON`, `INBOX_CRON`, `MAINTENANCE_CRON` | see above | Standard 5-field cron |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | — | Only to use a different OpenAI-compatible endpoint instead of the bundled Ollama |
| `OLLAMA_URL` | set by `docker-compose.yml` | Where the bundled Ollama runs (`http://ollama:11434`) |
| `SEARXNG_URL` | set by `docker-compose.searxng.yml` | Your SearXNG, for the AI's web look-ups (`http://searxng:8080`) |
| `OLLAMA_API_KEY` | — | Optional: use Ollama's hosted web search for look-ups (instead of, or as a fallback to, SearXNG) |
| `OLLAMA_TAG` | `latest` | (Compose) pin the bundled Ollama to a published version, e.g. `0.40.13` |
| `COMPBOT_UID` / `COMPBOT_GID` | `568` | (Compose) user compbot runs as; must own `./data` |
| `COMPOSE_FILE` | `docker-compose.yml` | (Compose) add your GPU file and the web search file, see the AI section |
| `TZ` | `Europe/London` | |
| `DATA_DIR` | `/data` | |

GUI settings include the daily entry cap (default 25; it counts successful entries, so blocked, failed or CAPTCHA attempts don't use it up, and a run keeps going until the queue is done or the cap is reached), delays between entries, retries, how to treat social and Gleam comps, skip keywords (e.g. `Glasgow, Leeds` for local-only prizes), minimum prize value, marketing opt-ins (newsletter comps always opt in, because that *is* the entry; third-party marketing is never ticked unless required), T&C respect, and auto-confirm links.

### AI helper (Ollama, runs on your NAS)

`docker-compose.yml` runs [Ollama](https://ollama.com) next to compbot, so the AI is local and nothing leaves your network (unless you set up web look-ups, below, which send out search words only). With a model chosen in **Settings → AI helper**, compbot will:

* **answer competition questions the MSE post didn't**: it reads the competition page first, then (with web look-ups set up, below) searches the web, then uses the model's general knowledge, and only uses answers the model is at least 80% sure of (adjustable). By default a second pass double-checks each answer and rejects ones it thinks are wrong. In testing, this caught a model that answered the dial's colour when asked about the case. Every AI answer, its confidence and the check result are shown on the competition page;
* optionally write short tie-breaker / "tell us why" answers (off by default);
* map form fields the rules don't recognise (it sees field **labels only** and replies with profile *keys*; your details are filled in locally and never sent to the model);
* read unclear posts, and optionally double-check possible-win emails.

**GPU acceleration.** Pick one line for `COMPOSE_FILE` in `.env`, then run `sudo docker compose up -d`:

| GPU | `.env` line | TrueNAS prerequisite |
|---|---|---|
| NVIDIA (GTX 9xx and newer) | `COMPOSE_FILE=docker-compose.yml:docker-compose.gpu-nvidia.yml` | **Apps → Configuration → Settings → Install NVIDIA Drivers**, then check `nvidia-smi` works in the shell |
| AMD Radeon RX 6800+ / 7000 / 9000 (ROCm) | `COMPOSE_FILE=docker-compose.yml:docker-compose.gpu-amd.yml` | none (uses `/dev/kfd` + `/dev/dri`) |
| Intel Arc / Iris Xe, older AMD (Vulkan) | `COMPOSE_FILE=docker-compose.yml:docker-compose.gpu-vulkan.yml` | none (uses `/dev/dri`) |

**Older NVIDIA cards (GTX 9xx / 10xx) use Vulkan.** Ollama's CUDA build only supports these cards with NVIDIA driver 570 or newer, and TrueNAS ships 550. So the bundled Ollama is compbot's own image, `ghcr.io/rk110038/compbot:ollama-latest`: official Ollama plus the pieces NVIDIA's Vulkan driver needs inside a container. The NVIDIA file passes the driver's `graphics` libraries through to it. Newer cards and drivers still use CUDA, because Ollama prefers it when both see the GPU. The image is rebuilt daily from the latest Ollama release (`.github/workflows/ollama-image.yml`). To check which one your card is using:

```bash
sudo docker logs compbot-ollama 2>&1 | grep -iE "compbot-ollama|inference compute"   # look for library=Vulkan or CUDA
sudo docker exec compbot-ollama vulkaninfo --summary                                 # should list your GeForce card
```

**Choose a model** in **Settings → AI models → Download** (progress is shown there), then select it under **AI helper → Model** and **Save**. Or from the shell: `sudo docker exec compbot-ollama ollama pull gemma4:12b`.

| Model | Download | Fits | Notes |
|---|---|---|---|
| `gemma4:12b` | 7.7 GB | 10–12 GB VRAM | **Recommended**: strong general knowledge and reading comprehension |
| `qwen3.5:9b` | 6.6 GB | 8 GB VRAM | Good for 8 GB cards |
| `qwen3.5:4b` | 3.3 GB | 4–6 GB VRAM or CPU | Small and quick. In testing it answered page questions well but needed the double-check |
| `gemma4:26b` / `qwen3.5:27b` | 16–17 GB | 24 GB VRAM | Most knowledgeable |

Use **Test the chosen model** in Settings to see an answer, how long it took, and whether the model ran on the GPU, the CPU or both. You can also check that `sudo docker exec compbot-ollama ollama ps` shows `100% GPU` while a model is loaded; Settings also shows "% on GPU". Without a GPU it still works, at about 20–30 seconds per question for a 4B model. If the test says **"signal: killed"**, Linux ran out of RAM and stopped the model: usually it was loading into ordinary memory instead of the graphics card. On TrueNAS the ZFS cache can take nearly all the RAM, so consider capping it (for example to 8 GiB with a *Post Init* command in **System → Advanced → Init/Shutdown Scripts**: `echo 8589934592 > /sys/module/zfs/parameters/zfs_arc_max`).

**Web look-ups.** Small models often "know" trivia confidently and wrongly, so compbot can search the web for any question the competition page doesn't answer. The AI writes the search words from the question (naming the brand or product the page is about), reads the top results' snippets, and answers again. The double-check sees the same results, and the competition page shows what was searched and which sites answered. Google's own search API is closed to new users and shuts down on 1 January 2027, so there are two ways to set this up:

* **SearXNG (recommended, free, no account).** A private metasearch engine that runs next to compbot and asks Google, Brave, DuckDuckGo and others for you. With Compose, add `:docker-compose.searxng.yml` to the end of `COMPOSE_FILE` in `.env`, then run `sudo docker compose up -d`. `truenas-app.yaml` includes it already.
* **Ollama's web search.** Create an API key at [ollama.com/settings/keys](https://ollama.com/settings/keys) (needs an ollama.com account; check your usage there) and set `OLLAMA_API_KEY`. If SearXNG is set up as well, this is only used when SearXNG finds nothing.

Then use **Settings → AI helper → Test web search**. Only the search words leave your network (never your details), result pages aren't opened, and results are cached for a fortnight, so retries don't search again. Each looked-up question costs one search and one extra AI pass. Questions answered from the page are not looked up.

**Slow disk.** compbot's database lives on the same pool as your other apps. If one of them floods the pool with writes, compbot shows a 🐢 banner saying the disk is slow. It also won't start new runs while a database write is stuck, and pages show "Database busy" instead of hanging. These clear on their own once the disk catches up.

## Privacy & safety

* Your details live only in the SQLite database on your dataset.
* The GUI requires a password. POSTs are checked for same-origin. Screenshots are only served to signed-in users.
* Win emails are never auto-confirmed as wins: you confirm them. Emails that ask for fees, bank details or crypto are flagged as **scam suspect**. Genuine promoters never charge you to claim a prize.
* compbot polls the MSE API once a day with a 1.5 s pause between pages, and spaces out entries (20–60 s by default).
* Links from forum posts and emails are untrusted. The browser, T&C fetches and confirmation clicks refuse private/LAN/loopback addresses, so a malicious link can't reach your NAS or home network.
* With web look-ups on, the only thing sent out is the search words the AI writes for a competition question. The AI never sees your details, and search result pages aren't opened.
* Entries that were submitted but showed no confirmation message are flagged **check entry**, with before/after screenshots, rather than silently counted.

## Local development

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
python -m playwright install chromium
$env:DATA_DIR = "$PWD\data"; $env:WEB_PASSWORD = "dev"
python -m pytest -q                       # parser, form mapper, inbox classifier, and browser tests against local fixtures
python -m uvicorn app.main:app --port 8765
```

Project layout:

```
app/
  sources/mse.py     MSE forum API client
  analyse.py         title/post parsing and triage rules
  entry/forms.py     form-field → profile/answer heuristics, T&C clause detector (pure, unit-tested)
  entry/browser.py   Playwright engine: cookies, CAPTCHA/bot-block detection, form choice, fill, submit, verify
  entry/emailer.py   email entries + notifications (Gmail SMTP)
  inbox.py           Gmail IMAP fetch + win/loss/confirm/scam classifier + comp matching
  jobs.py            discover / enter / inbox / maintenance jobs
  main.py            FastAPI GUI, auth, scheduler
  llm.py             optional OpenAI-compatible client
  websearch.py       optional web look-ups for the AI (SearXNG or Ollama's web search), with a two-week cache
```

## Troubleshooting

* **Lots of `blocked` / `captcha` results.** That's the sites saying no to bots. They're in the manual queue.
* **`needs_input`.** A required field compbot couldn't map (often a tie-breaker or an unknown answer). Add the answer on the competition page and click **Re-queue**, or enter it yourself.
* **Inbox shows nothing.** Check the Gmail app password and that `GMAIL_ADDRESS` matches the account. Look in **Log** for IMAP errors.
* **Chromium crashes.** Make sure `shm_size: 1gb` is in the YAML and the app has at least 1–2 GB RAM.
* **Permission denied on /data.** The dataset must be writable by UID/GID 568 (the TrueNAS *apps* user).
