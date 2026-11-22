# ZRPL Investing

*(Formerly the Medical catalyst radar. The repository keeps its old name, so the web address doesn't change.)*

A free, self-updating website for running Z Rahim Pty Ltd's value portfolio across all 11 ASX sectors, using the Castles, Moats and Lords method. It runs on your own GitHub account and doesn't need Claude.

- **Three sections:** Rare disease · Medical devices · Other pharma, biotech & medical
- **For each stock:** probability of success, management and spending efficiency, anticipated market reaction, and buy/sell prices from your own rules
- **Catalyst follow-up:** a calendar, overdue warnings, and a promise tracker
- **Robot runs twice each weekday** (8:47 am and 5:17 pm Brisbane time). It fetches prices and ASX announcements, reads quarterly 4C cash-flow reports, records how each stock moved after news, and sends you alerts.
- **Flag-and-confirm:** the robot never changes your assumptions. It puts suggestions in the **Review queue**, and you apply or dismiss each one.

Cost: **A$0**. GitHub Pages and GitHub Actions are free for public repositories.

---

## One-time setup (about 20 minutes)

### 1. Create the repository
1. Sign up at https://github.com (free).
2. Click **+ → New repository**. Name it `biotech-radar`, choose **Public**, then click **Create repository**.
3. On the new repository page, click **uploading an existing file**. Upload the *unzipped* files, not the .zip. Drag in **everything inside** the `biotech-radar` folder from the zip: `index.html`, `README.md`, and the `data`, `scripts` and `.github` folders. Click **Commit changes**.
   - Hidden folders: if your computer hides the `.github` folder, create the file by hand instead. Click **Add file → Create new file**, type the name `.github/workflows/update.yml`, paste in the contents of that file, and commit.

### 2. Turn on the website
**Settings → Pages →** Source: *Deploy from a branch* → Branch: `main`, folder `/ (root)` → **Save**.
About a minute later your radar is live at `https://YOUR-USERNAME.github.io/biotech-radar/`.

### 3. Let the robot write and send alerts
**Settings → Actions → General →** Workflow permissions: **Read and write permissions** → **Save**.

### 4. Run it the first time
Go to **Actions → Update radar → Run workflow**. After about 2 minutes, refresh your radar. You should see prices, announcements, the Review queue and a first alert summary.
- **Email alerts:** each run that finds something opens a GitHub *issue* that mentions you, and GitHub emails you. To confirm email is set up, check that **Settings (your profile) → Notifications → Email** is ticked for "Participating, @mentions".
- **Phone alerts (optional, free):**
  1. Install the **ntfy** app (iOS or Android).
  2. In the app, subscribe to a long random topic name, e.g. `radar-k39fj2lq8x`.
  3. On GitHub, go to repo **Settings → Secrets and variables → Actions → New repository secret**. Name it `NTFY_TOPIC`, and set the value to the topic name from step 2.

### 5. Let the page save your edits (needs a token)
1. Go to GitHub **profile picture → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Under **Repository access**, choose *Only select repositories* and pick `biotech-radar`.
3. Set **Permissions:** *Contents* → Read and write, and *Actions* → Read and write.
4. Generate the token, copy it, and paste it into the radar under **Settings & positions → GitHub connection**, then click **Test**.

The token is stored only in that browser. Repeat this step on each device you use, or skip it on devices where you only read.

### 6. Enter your positions
In the radar, go to **Settings & positions → Import positions file** and choose `my-positions.json` (supplied separately, **not** in the repository). It holds your Neuren holding of 38,000 shares at A$15.40 and the 4-step sell ladder from your exit plan. Add the other holdings by hand.

Positions are kept on your device. Under **What goes to GitHub** you choose what the robot can see:
- **Ladder prices only (default).** Only your sell-ladder step names and prices are saved to the repository. The robot emails you when one is reached. Your share counts, cost and dates never leave your device. Anyone reading the repository would see a few price levels, not your holdings.
- **Nothing.** Ladder alerts appear only on the page.
- **Everything.** This adds holding-time-limit alerts, but your holdings become public if the repository is public.

**Your own prices:** on any stock's page, the Decision box has *Set your own: buy ≤ / sell ≥*. Leave these blank to use your rules; fill them in to override. Sell-ladder prices are edited in *Your position*.

### 7. Review the draft assumptions
Every stock starts as **Draft**. Open each one and check its scenarios, which drive the buy/sell prices, then tick *I've reviewed these*.

---

## How the numbers work

| What | How it's calculated |
|---|---|
| Expected value | Probability-weighted average of your scenario values per share at the horizon date |
| Rare disease buy price | Success-case value ÷ 10. If it works, buying at this price gives your 10x. The buy price is also capped at the expected value. |
| Other sections buy price | Expected value discounted at 15% a year to the horizon |
| Sell / trim price | Expected value discounted at 5% a year. Above this, the expected return is below 5% a year. |
| Probability of success | Base rate for the area × stage, adjusted by modifiers (e.g. "open-label only" ×0.6 odds, "design agreed with FDA" ×1.3). All are editable in Settings. |
| Management grade | Promises kept 40% · cash runway and dilution 30% · scorecard 30% |
| Scorecards | Rare disease: built from public descriptions of David R. Craig, *The New Rules of Rare Disease* (2026). Devices and other: Mehta, *Commercializing Successful Biomedical Technologies* (2nd ed.). |
| Market reaction | Rough base rates for each type of event. The page also shows each stock's own measured one-day and five-day moves, which build up over time. |

## Adding a stock
Open the section → **+ Add stock** → fill in the scenarios, trials, catalysts and keywords → **Save changes**. Prices arrive at the next update; click **Update now** to fetch them straight away.

## If something breaks
The robot uses the same public data feeds as the ASX website and Yahoo Finance. These are free but unofficial, so they can change without notice. Problems show under **Settings & positions → Robot status** and in the **Actions** tab. Copy the error and ask Claude to fix `scripts/update.py`.

To check the robot logic offline, run `python scripts/test_update.py`.

## Files
- `data/companies.json`, `data/config.json`, `data/decisions.json`: yours (edited from the page)
- `data/prices.json`, `announcements.json`, `proposals.json`, `reactions.json`, `alerts.json`, `status.json`: written by the robot

*Decision support built on your own assumptions, not financial advice.*


---

## What's new in ZRPL Investing (Stage 1)
- **Sector sidebar:** all 11 ASX sectors. Health Care keeps its three sub-sections.
- **Home & value:** my dated view of where value is (refreshed each Monday), what needs attention, the 10-stock plan, and the A$500k paper portfolio against the VAS benchmark.
- **Animal rulebooks:** each stock has an animal (🐻 🐘 🐍 🐢 🐒 🐜) and a sleeve. The checks and buy rule change with the animal: PEG, EV/EBIT, FCF yield, return on capital, price/NAV and liquidity.
- **Thesis card:** castle (5 C's), moats, lords, the one-paragraph thesis, the misperception, what would prove us wrong, and a pre-mortem. **Mark thesis broken** triggers a sell alert.
- **Sell alerts with a limit price** for holdings (real or paper), by email.
- **Positions privacy:** by default GitHub only learns *which* stocks you hold, the first-bought date and ladder prices. Share counts and costs stay on your device.
- **Your data:** company records saved by the old radar are converted automatically the first time the new page opens. Nothing you entered is lost.

Coming next: Stage 2 (trade log, decision journal, dividends and franking, encrypted) and Stage 3 (Monday research by Claude).
