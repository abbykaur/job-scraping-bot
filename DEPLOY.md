# Deploying the CS Jobs Bot (free, via GitHub Actions)

The bot runs **once per invocation** (ingest → post → exit) and is scheduled
**3× per weekday** by GitHub Actions. `jobs.db` is committed back after each run
so the `posted` flag persists — **a job posted once is never posted again.**

## One-time setup

1. **Create a GitHub repo and push this folder:**
   ```bash
   git init
   git add .
   git commit -m "CS jobs bot"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
   > `jobs.db` is committed intentionally (it's the persistence store).
   > `.env` is gitignored — your token never goes to GitHub.

2. **Add secrets** in the repo: Settings → Secrets and variables → Actions → New repository secret. Add three:
   | Secret | Value |
   |--------|-------|
   | `DISCORD_TOKEN` | your bot token |
   | `CHANNEL_ID` | security-feed channel ID |
   | `CHANNEL_ID_AIML` | AI/ML-feed channel ID |

3. **Enable Actions write access** (needed to commit `jobs.db` back):
   Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.

4. **Test it now:** Actions tab → "post-jobs" → **Run workflow** (manual trigger).
   Watch it post to Discord, then confirm a follow-up commit "Update jobs.db" appears.

## Schedule (3× weekday, Pacific)

| Run | Pacific | Purpose |
|-----|---------|---------|
| Morning | 7:30 AM | early recruiter drops |
| Midday | 1:00 PM | after-lunch updates |
| Evening | 5:30 PM | end-of-day cleanups |

## ⚠️ Two things to remember

- **Daylight saving (manual, ~twice a year).** GitHub cron is UTC-only; the times
  in `.github/workflows/jobs.yml` are set for **PDT (summer)**. When the Bay Area
  switches to **PST in early November**, add **+1 hour** to each UTC cron hour, or
  runs fire an hour early. Reverse it in March. (Winter values are noted in comments.)

- **60-day inactivity pause.** GitHub disables scheduled workflows if the repo has
  no commits for 60 days. The bot's own `jobs.db` commits count as activity, so as
  long as it's posting it stays alive — but if it goes quiet, push any commit to
  re-enable.

## Local run (for testing)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# put DISCORD_TOKEN / CHANNEL_ID / CHANNEL_ID_AIML in .env
python bot.py     # one cycle, then exits
```
