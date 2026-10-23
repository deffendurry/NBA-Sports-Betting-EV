#!/usr/bin/env python3
"""Daily NBA prop pipeline.
Caesars lines (via OddsPapi) + Basketball-Reference game logs -> docs/bets.json

Usage:
  ODDSPAPI_KEY=... python pipeline.py                # normal daily run
  ODDSPAPI_KEY=... python pipeline.py --list-markets # show NBA prop market names/IDs
  ODDSPAPI_KEY=... python pipeline.py --inspect      # dump one game's raw Caesars JSON
"""
import argparse, json, os, re, sys, time, unicodedata
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from statistics import mean, pstdev

import pandas as pd
import requests
from bs4 import BeautifulSoup

ODDS_API = "https://api.oddspapi.io/v4"
BR = "https://www.basketball-reference.com"
NBA_SPORT_ID = 11
ROOT = Path(__file__).parent
DATA, DOCS, LOGS = ROOT / "data", ROOT / "docs", ROOT / "data" / "logs"
for d in (DATA, DOCS, LOGS):
    d.mkdir(parents=True, exist_ok=True)

STATS = {"pts": ["PTS"], "reb": ["TRB"], "ast": ["AST"], "3pm": ["3P"],
         "pra": ["PTS", "TRB", "AST"], "pr": ["PTS", "TRB"],
         "pa": ["PTS", "AST"], "ra": ["TRB", "AST"]}
LINE_KEYS = ("line", "handicap", "point", "points", "total", "specialBetValue")
EXCLUDE = ("quarter", "half", "1st", "2nd", "3rd", "4th", "first", "double",
           "triple", "alternate", "alt ", "to record", "or more", "race", "most")
UA = {"User-Agent": "Mozilla/5.0 (personal NBA prop research script)"}

odds_calls = 0
_last_br = 0.0


# ---------- helpers ----------
def now():
    return datetime.now(timezone.utc)


def current_season():
    n = now()
    return n.year + 1 if n.month >= 10 else n.year  # BR labels 2026-27 as 2027


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", "", s.replace("-", " "))
    return " ".join(t for t in s.split() if t not in {"jr", "sr", "ii", "iii", "iv", "v"})


def load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def is_fresh(obj, hours):
    if not obj:
        return False
    if hours is None:
        return True
    try:
        t = datetime.fromisoformat(obj["fetched"])
        return (now() - t) < timedelta(hours=hours)
    except Exception:
        return False


# ---------- OddsPapi ----------
def op(path, **params):
    global odds_calls
    params["apiKey"] = os.environ["ODDSPAPI_KEY"]
    r = requests.get(f"{ODDS_API}{path}", params=params, timeout=30)
    odds_calls += 1
    if r.status_code == 429:
        sys.exit("OddsPapi rate/quota limit hit (429). Free tier is 250 requests/month.")
    r.raise_for_status()
    time.sleep(1.0)
    return r.json()


def classify(name):
    n = name.lower()
    if "player" not in n or any(x in n for x in EXCLUDE):
        return None
    if re.search(r"three|3[- ]?p", n) and "rebound" not in n and "assist" not in n:
        return "3pm"
    has = frozenset(k for k, w in (("p", "point"), ("r", "rebound"), ("a", "assist")) if w in n)
    return {frozenset("p"): "pts", frozenset("r"): "reb", frozenset("a"): "ast",
            frozenset("pr"): "pr", frozenset("pa"): "pa", frozenset("ra"): "ra",
            frozenset("par"): "pra"}.get(has)


def get_markets(force=False):
    path = DATA / "markets_nba.json"
    cache = load_json(path)
    if not force and is_fresh(cache, 24 * 7):
        return cache["markets"]
    markets = op("/markets", sportId=NBA_SPORT_ID)
    path.write_text(json.dumps({"fetched": now().isoformat(), "markets": markets}))
    return markets


def side_of(*texts):
    for t in texts:
        if isinstance(t, str):
            tl = t.lower()
            if "over" in tl:
                return "over"
            if "under" in tl:
                return "under"
    return None


def first_num(d, keys):
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def parse_fixture(odds, market_stat, labels, skipped):
    """Return {(player, stat, line): {'over': dec, 'under': dec}} from Caesars."""
    out = {}
    book = ((odds.get("bookmakerOdds") or {}).get("caesars") or {}).get("markets") or {}
    for mid, mdata in book.items():
        stat = market_stat.get(str(mid))
        if not stat:
            continue
        for oid, odata in (mdata.get("outcomes") or {}).items():
            for pid, p in (odata.get("players") or {}).items():
                if not isinstance(p, dict) or not p.get("active", True):
                    continue
                name = p.get("playerName")
                price = p.get("price")
                line = first_num(p, LINE_KEYS) or first_num(odata, LINE_KEYS) or first_num(mdata, LINE_KEYS)
                side = side_of(labels.get(str(mid), {}).get(str(oid)), p.get("outcomeName"),
                               odata.get("outcomeName"), odata.get("name"), p.get("name"))
                if not (name and price and price > 1 and line is not None and side):
                    skipped["unparsed"] = skipped.get("unparsed", 0) + 1
                    continue
                out.setdefault((name, stat, line), {})[side] = float(price)
    return out


# ---------- Basketball-Reference ----------
def br_get(url, delay):
    global _last_br
    wait = delay - (time.time() - _last_br)
    if wait > 0:
        time.sleep(wait)
    r = requests.get(url, headers=UA, timeout=30)
    _last_br = time.time()
    if r.status_code == 404:
        return None
    if r.status_code in (403, 429):
        raise RuntimeError(f"Basketball-Reference blocked the request ({r.status_code}). "
                           "Slow down, or run from a different network.")
    r.raise_for_status()
    return r.text


def player_index(season, delay):
    path = DATA / f"index_{season}.json"
    cache = load_json(path)
    if is_fresh(cache, None if season < current_season() else 24):
        return cache["players"]
    html = br_get(f"{BR}/leagues/NBA_{season}_per_game.html", delay)
    players = {}
    if html:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select('table#per_game_stats td a[href^="/players/"]'):
            m = re.search(r"/players/\w/(\w+)\.html", a["href"])
            if m:
                players[norm(a.text)] = m.group(1)
    path.write_text(json.dumps({"fetched": now().isoformat(), "players": players}))
    return players


def game_log(pid, season, delay):
    path = LOGS / f"{pid}_{season}.json"
    cache = load_json(path)
    if is_fresh(cache, None if season < current_season() else 20):
        return cache["games"]
    games = []
    html = br_get(f"{BR}/players/{pid[0]}/{pid}/gamelog/{season}", delay)
    if html:
        try:
            df = pd.read_html(StringIO(html), attrs={"id": "pgl_basic"})[0]
            for c in ("PTS", "TRB", "AST", "3P"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["PTS", "TRB", "AST", "3P"])
            games = [{"PTS": int(r.PTS), "TRB": int(r.TRB), "AST": int(r.AST), "3P": int(r["3P"])}
                     for _, r in df.iterrows()]
        except Exception:
            games = []
    path.write_text(json.dumps({"fetched": now().isoformat(), "games": games}))
    return games


def projection(prev, cur, stat):
    cols = STATS[stat]
    f = lambda gs: [sum(g[c] for c in cols) for g in gs]
    cv, av = f(cur), f(prev + cur)
    if len(av) < 5:
        return None
    season = mean(cv) if len(cv) >= 8 else mean(av[-40:])
    return {"season_avg": round(season, 2), "l10_avg": round(mean(av[-10:]), 2),
            "sd": round(max(pstdev(av[-25:]), 0.75), 2), "n": len(cv)}


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--list-markets", action="store_true")
    ap.add_argument("--hours", type=float, default=15, help="fixture window from now")
    ap.add_argument("--max-games", type=int, default=12, help="cap odds calls (free tier = 250/mo)")
    ap.add_argument("--max-players", type=int, default=200)
    ap.add_argument("--br-delay", type=float, default=7.0, help="seconds between BR requests")
    a = ap.parse_args()
    if "ODDSPAPI_KEY" not in os.environ:
        sys.exit("Set the ODDSPAPI_KEY environment variable.")

    markets = get_markets(force=a.list_markets)
    if a.list_markets:
        for m in markets:
            n = m.get("marketName", "")
            if re.search(r"point|rebound|assist|three|3", n, re.I):
                print(m.get("marketId"), "|", n, "|", classify(n))
        return
    market_stat = {str(m["marketId"]): classify(m.get("marketName", "")) for m in markets}
    market_stat = {k: v for k, v in market_stat.items() if v}
    labels = {}
    for m in markets:
        outs = m.get("outcomes")
        if isinstance(outs, list):
            labels[str(m["marketId"])] = {str(o.get("outcomeId")): o.get("outcomeName") for o in outs}
    print(f"Prop markets recognised: {len(market_stat)}")
    if not market_stat:
        sys.exit("No player-prop markets recognised. Run --list-markets and adjust classify().")

    t0 = now()
    fx = op("/fixtures", sportId=NBA_SPORT_ID, status="prematch",
            **{"from": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "to": (t0 + timedelta(hours=a.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    fx = [f for f in fx if f.get("tournamentSlug") == "nba" and f.get("hasOdds")][: a.max_games]
    print(f"NBA fixtures with odds: {len(fx)}")

    props, skipped, games_out = {}, {}, []
    for f in fx:
        odds = op("/odds", fixtureId=f["fixtureId"], bookmakers="caesars")
        if a.inspect:
            (DATA / "raw_odds_sample.json").write_text(json.dumps(odds, indent=1)[:400000])
            print("Saved data/raw_odds_sample.json. Send me the first ~80 lines to adapt the parser.")
            return
        label = f"{f.get('participant1Name')} vs {f.get('participant2Name')}"
        parsed = parse_fixture(odds, market_stat, labels, skipped)
        games_out.append(label)
        for k, v in parsed.items():
            if "over" in v and "under" in v:
                props[k] = {**v, "game": label, "start": f.get("startTime")}
    print(f"Props with both sides: {len(props)} | skipped: {skipped} | odds API calls: {odds_calls}")

    season = current_season()
    idx = {**player_index(season - 1, a.br_delay), **player_index(season, a.br_delay)}
    names = sorted({k[0] for k in props})[: a.max_players]
    stats_cache, unmatched = {}, []
    for i, name in enumerate(names, 1):
        pid = idx.get(norm(name))
        if not pid:
            unmatched.append(name)
            continue
        try:
            prev = game_log(pid, season - 1, a.br_delay)
            cur = game_log(pid, season, a.br_delay)
        except RuntimeError as e:
            print(e)
            break
        stats_cache[name] = (prev, cur)
        print(f"[{i}/{len(names)}] {name}: {len(cur)} games this season")

    out = []
    for (name, stat, line), v in props.items():
        if name not in stats_cache:
            continue
        proj = projection(*stats_cache[name], stat)
        if proj:
            out.append({"player": name, "stat": stat, "line": line, "over": v["over"],
                        "under": v["under"], "game": v["game"], "start": v["start"], **proj})
    (DOCS / "bets.json").write_text(json.dumps({
        "generated": now().isoformat(), "source": "OddsPapi (Caesars) + Basketball-Reference",
        "games": games_out, "unmatched": unmatched, "props": out}, indent=1))
    print(f"Wrote docs/bets.json with {len(out)} props ({len(unmatched)} players unmatched).")


if __name__ == "__main__":
    main()
