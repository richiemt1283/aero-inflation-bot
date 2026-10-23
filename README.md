# AERO inflation bot

Weekly on-chain snapshot of AERO total supply → realized annualized inflation → Telegram.

Verdict bands: 🟢 < 8% · 🟡 8–12% · 🔴 ≥ 12% (old Aerodrome rate).

## 1. Local setup (macOS / zsh)

```bash
cd aero-inflation-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in Telegram token + chat id
python aero_inflation.py --self-test   # offline sanity check
python aero_inflation.py --check       # confirms the AERO address returns symbol "AERO"
python aero_inflation.py --dry-run     # prints a report, writes nothing
python aero_inflation.py               # first real snapshot (baseline)
```

Run it once **now** to set the baseline. Week-over-week numbers appear from the second run.

## 2. Run it every week on GitHub (works with the laptop off)

```bash
git init && git add . && git commit -m "aero inflation bot"
gh repo create aero-inflation-bot --private --source=. --push
gh secret set AERO_TOKEN_BASE --body "0x..."
gh secret set BASE_RPC_URL --body "https://mainnet.base.org"
gh secret set TELEGRAM_BOT_TOKEN      # paste when prompted
gh secret set TELEGRAM_CHAT_ID        # paste when prompted
gh workflow run "AERO weekly inflation"   # test it now
```

Runs every Monday 08:07 Mexico City and commits `data/supply_history.csv` back to the repo.

## Known limits

- **Contract address**: confirm the post-merger AERO address on Base (`--check` must print `AERO`).
- **xAERO**: rewards on non-Base chains mint xAERO there. Add those addresses to `.env` when published, or Base-only supply understates inflation.
- **Mint-on-claim**: the first weeks read low until LPs claim. Trust the 4-week number, not the 1-week.
- Public RPCs can rate-limit; swap in an Alchemy/Infura URL if calls fail.

## Windows (PowerShell)

```powershell
cd aero-inflation-bot
py -m venv venv
.\venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env
notepad .env                      # fill in Telegram token + chat id
python aero_inflation.py --self-test
python aero_inflation.py --check
python aero_inflation.py
```

The GitHub section above works the same on Windows once Git and GitHub CLI are installed:
`winget install Git.Git GitHub.cli`, then `gh auth login`.
