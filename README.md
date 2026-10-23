# NBA +EV Props (Caesars via OddsPapi + Basketball-Reference)

Every day GitHub Actions runs `pipeline.py`, which writes `docs/bets.json`. GitHub Pages serves `docs/index.html`, which reads that file.

## One-time setup
1. Sign up at oddspapi.io and copy your free API key.
2. Create a new **private or public** GitHub repo and upload everything in this folder (keep the `.github/workflows/daily.yml` path).
3. Repo **Settings → Secrets and variables → Actions → New repository secret**: name `ODDSPAPI_KEY`, value = your key.
4. Repo **Settings → Pages**: Source = "Deploy from a branch", Branch = `main`, folder = `/docs`.
5. **Actions tab → Daily NBA props → Run workflow** for the first run. It also runs daily at 15:00 UTC.

## First-run checks (important)
The OddsPapi player-prop JSON shape for NBA over/unders was not verified. Before relying on it, run locally:
```
pip install -r requirements.txt
export ODDSPAPI_KEY=your_key
python pipeline.py --list-markets   # confirm points/rebounds/assists markets are recognised
python pipeline.py --inspect        # saves data/raw_odds_sample.json for one game
```
If props come back as "unparsed", send the first ~80 lines of `raw_odds_sample.json` and the parser can be adjusted.

## Limits to know about
- **OddsPapi free tier: 250 requests/month.** Each daily run uses 1 (fixtures) + 1 per game. With ~8 games that is ~270/month, so use `--max-games`, skip off days, or upgrade.
- **Basketball-Reference** rate-limits to 20 requests/minute (blocked sessions can be jailed for a day). The script waits 7s between requests and caches game logs in `data/`. The first run is slow (about 2 requests per player); later runs only refresh current-season logs.
- BR may block GitHub's datacenter IPs (403). If so, run the pipeline from your own computer, or switch the stats source to `nba_api`.
- Check each provider's terms of service for your use.
