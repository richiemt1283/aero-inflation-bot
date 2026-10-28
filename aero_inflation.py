#!/usr/bin/env python3
"""
AERO weekly inflation tracker.

Reads AERO total supply on-chain (Base AERO + optional xAERO receipt tokens on
other chains), stores a snapshot, computes realized annualized inflation, and
sends a Telegram report.

Usage:
  python aero_inflation.py              # take snapshot, compute, send Telegram
  python aero_inflation.py --dry-run    # print report, don't write or send
  python aero_inflation.py --check      # verify RPCs + contracts, print symbols/supply
  python aero_inflation.py --self-test  # offline test with fake data
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv optional in CI (secrets come from env)
    pass

# Windows consoles may not be UTF-8; keep emoji output from crashing
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
HISTORY = Path(os.getenv("HISTORY_FILE", HERE / "data" / "supply_history.csv"))

# Function selectors
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"
SEL_BALANCE_OF = "0x70a08231"

# Thesis thresholds (annualized realized inflation)
WATCH = float(os.getenv("INFLATION_WATCH", "0.08"))   # above team's ~8% projection
BROKEN = float(os.getenv("INFLATION_BROKEN", "0.12"))  # back to old Aerodrome rate
# A single run annualizing above this is treated as a one-off mint (VELO->AERO migration)
SPIKE = float(os.getenv("SPIKE_THRESHOLD", "0.40"))

# Chains that may hold xAERO. Each needs <CHAIN>_RPC_URL and XAERO_<CHAIN> in .env
CAP_MULTIPLE = float(os.getenv("CAP_MULTIPLE", "1.2"))   # Aero's long-run rewards/revenue target
STAKED_SHARE = float(os.getenv("STAKED_SHARE", "0.5"))   # ~half of supply is sAERO per Aero
COINGECKO_ID = os.getenv("COINGECKO_ID", "aerodrome-finance")
DEFILLAMA_SLUGS = [x.strip() for x in os.getenv("DEFILLAMA_SLUGS", "aero,aerodrome").split(",") if x.strip()]

# Off by default: the new AERO launched with its full supply pre-minted (~2.12B), so migration
# moves existing tokens instead of minting new ones. Supply growth = emissions only.
MIGRATION_MODE = os.getenv("MIGRATION_MODE", "0") == "1" and bool(
    os.getenv("LEGACY_AERO_TOKEN") and os.getenv("AERO_MIGRATION_ADDRESS"))
# Aerodrome final supply 2.0026B = 94.5% of merged token -> ~2.119B total at launch
ECON_BASE = float(os.getenv("ECON_BASE", "2119000000"))
VELO_TOKEN_OP = os.getenv("VELO_TOKEN_OP", "0x9560e827aF36c94D2Ac33a39bCE1Fe78631088Db")
VELO_MIGRATION_OP = os.getenv("VELO_MIGRATION_OP", "0x6daE95FE833f190640dB1176F8C8a2e50C6a66c0")
VELO_RATIO = float(os.getenv("VELO_RATIO", "0.044"))

OTHER_CHAINS = ["BASE", "ETHEREUM", "OPTIMISM", "ARBITRUM", "INK", "ROBINHOOD", "ARC"]  # xAERO receipt tokens


@dataclass
class Snapshot:
    ts: datetime
    base_supply: float
    xaero_supply: float
    foundation_balance: float | None
    excluded: float = 0.0  # cumulative one-off mints removed (e.g. VELO migration)
    price: float | None = None      # AERO/USD
    rev_30d: float | None = None    # holder revenue, last 30 days, USD
    migrated: float = 0.0           # legacy AERO held by the migration contract (minted 1:1 as new AERO)

    @property
    def total(self) -> float:
        return self.base_supply + self.xaero_supply

    @property
    def adj_total(self) -> float:
        """Supply minus one-off mints and migrated tokens (= new AERO actually emitted, in migration mode)."""
        return self.total - self.excluded - self.migrated

    @property
    def econ(self) -> float:
        """Economic supply: what holders are diluted against.
        In migration mode = full post-merger supply (legacy AERO + VELO share) + emissions minted since."""
        return (ECON_BASE + self.adj_total) if MIGRATION_MODE else self.adj_total


# ---------------------------------------------------------------- RPC helpers
def rpc_call(url: str, to: str, data: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC error from {url}: {body['error']}")
    return body["result"]


def erc20_decimals(url: str, token: str) -> int:
    return int(rpc_call(url, token, SEL_DECIMALS), 16)


def erc20_total_supply(url: str, token: str) -> float:
    raw = int(rpc_call(url, token, SEL_TOTAL_SUPPLY), 16)
    return raw / 10 ** erc20_decimals(url, token)


def erc20_balance(url: str, token: str, holder: str) -> float:
    data = SEL_BALANCE_OF + holder.lower().replace("0x", "").rjust(64, "0")
    raw = int(rpc_call(url, token, data), 16)
    return raw / 10 ** erc20_decimals(url, token)


def erc20_symbol(url: str, token: str) -> str:
    res = rpc_call(url, token, SEL_SYMBOL)
    b = bytes.fromhex(res[2:])
    if len(b) >= 96:  # dynamic string
        length = int.from_bytes(b[32:64], "big")
        return b[64 : 64 + length].decode(errors="replace")
    return b.rstrip(b"\x00").decode(errors="replace")


# ---------------------------------------------------------------- snapshot
def take_snapshot() -> Snapshot:
    base_rpc = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    aero = os.getenv("AERO_TOKEN_BASE")
    if not aero:
        sys.exit("AERO_TOKEN_BASE is not set. Copy .env.example to .env and fill it in.")

    base_supply = erc20_total_supply(base_rpc, aero)

    xaero_total = 0.0
    for chain in OTHER_CHAINS:
        rpc = os.getenv(f"{chain}_RPC_URL") or (base_rpc if chain == "BASE" else None)
        token = os.getenv(f"XAERO_{chain}")
        if rpc and token:
            try:
                v = erc20_total_supply(rpc, token)
                XAERO_BY_CHAIN[chain.lower()] = v
                xaero_total += v
            except Exception as e:
                print(f"[xAERO {chain}] {e}")

    migrated = 0.0
    legacy, mig = os.getenv("LEGACY_AERO_TOKEN"), os.getenv("AERO_MIGRATION_ADDRESS")
    if MIGRATION_MODE and legacy and mig:
        try:
            migrated = erc20_balance(base_rpc, legacy, mig)
        except Exception as e:
            print(f"[migration] {e}")
        op_rpc = os.getenv("OPTIMISM_RPC_URL")
        if op_rpc and VELO_TOKEN_OP and VELO_MIGRATION_OP:
            try:
                velo = erc20_balance(op_rpc, VELO_TOKEN_OP, VELO_MIGRATION_OP)
                MIGRATION_INFO["velo_migrated"] = velo
                migrated += velo * VELO_RATIO
            except Exception as e:
                print(f"[velo migration] {e}")
        MIGRATION_INFO["legacy_total"] = migrated

    foundation = os.getenv("AERO_FOUNDATION_ADDRESS")
    fbal = erc20_balance(base_rpc, aero, foundation) if foundation else None

    return Snapshot(datetime.now(timezone.utc), base_supply, xaero_total, fbal,
                    price=fetch_price(), rev_30d=fetch_holder_rev_30d(), migrated=migrated)


XAERO_BY_CHAIN: dict = {}
MIGRATION_INFO: dict = {}


# ---------------------------------------------------------------- market data
def fetch_price() -> float | None:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": COINGECKO_ID, "vs_currencies": "usd"},
            timeout=20,
        )
        r.raise_for_status()
        return float(r.json()[COINGECKO_ID]["usd"])
    except Exception as e:
        print(f"[price] unavailable: {e}")
        return None


def fetch_holder_rev_30d() -> float | None:
    """Spike-resistant 30d holder revenue: median daily x 30.
    A single lump payment (e.g. Sep 9, 2026: $7.9M in one day) can double a plain 30d sum."""
    global LAST_REV_INFO
    for slug in DEFILLAMA_SLUGS:
        try:
            r = requests.get(
                f"https://api.llama.fi/summary/fees/{slug}",
                params={"dataType": "dailyHoldersRevenue"},
                timeout=30,
            )
            if r.status_code != 200:
                continue
            body = r.json()
            chart = body.get("totalDataChart") or []
            days = [float(v) for _, v in chart[-30:] if v is not None]
            if len(days) < 10:
                v = body.get("total30d")
                if v:
                    return float(v)
                continue
            med = sorted(days)[len(days) // 2]
            raw = sum(days)
            spikes = [v for v in days if med > 0 and v > 5 * med]
            LAST_REV_INFO = {"slug": slug, "raw30": raw, "median30": med * 30, "spikes": spikes}
            return med * 30
        except Exception as e:
            print(f"[defillama:{slug}] {e}")
    print("[revenue] unavailable from DefiLlama")
    return None


LAST_REV_INFO: dict = {}


# ---------------------------------------------------------------- dune emissions
DUNE_QUERY_ID = os.getenv("DUNE_QUERY_ID", "8980735")
DUNE_DATE_COL = os.getenv("DUNE_DATE_COL", "")
DUNE_USD_COL = os.getenv("DUNE_USD_COL", "")
DUNE_AERO_COL = os.getenv("DUNE_AERO_COL", "")


def _dune_get(path: str, key: str, **kw):
    r = requests.request(kw.pop("method", "GET"), f"https://api.dune.com/api/v1/{path}",
                         headers={"X-Dune-API-Key": key}, timeout=60, **kw)
    r.raise_for_status()
    return r.json()


def _dune_rows(key: str, refresh: bool) -> tuple[list[dict], list[str]]:
    import time
    if refresh:
        ex = _dune_get(f"query/{DUNE_QUERY_ID}/execute", key, method="POST")
        eid = ex["execution_id"]
        for _ in range(40):  # up to ~200s
            st = _dune_get(f"execution/{eid}/status", key)
            if st.get("state") == "QUERY_STATE_COMPLETED":
                break
            if st.get("state") in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                raise RuntimeError(f"Dune execution {st.get('state')}")
            time.sleep(5)
        body = _dune_get(f"execution/{eid}/results", key)
    else:
        body = _dune_get(f"query/{DUNE_QUERY_ID}/results", key, params={"limit": 5000})
    res = body.get("result") or {}
    return res.get("rows") or [], (res.get("metadata") or {}).get("column_names") or []


def _pick(cols: list[str], override: str, must: tuple[str, ...], prefer: tuple[str, ...] = (), avoid: tuple[str, ...] = ()) -> str | None:
    if override:
        return override if override in cols else None
    cands = [c for c in cols if all(m in c.lower() for m in must) and not any(x in c.lower() for x in avoid)]
    for pref in prefer:
        for c in cands:
            if pref in c.lower():
                return c
    return cands[0] if cands else None


def _to_date(v) -> datetime | None:
    if v is None:
        return None
    try:
        return datetime.fromisoformat(str(v).replace(" UTC", "").replace("Z", "+00:00")[:25].replace(" ", "T"))
    except ValueError:
        return None


EMISSIONS_CSV_URL = os.getenv("EMISSIONS_CSV_URL", "")


def _csv_rows(url: str) -> tuple[list[dict], list[str]]:
    import io
    if not url.lower().startswith("http"):  # local file, e.g. data/emissions.csv
        path = Path(url) if Path(url).is_absolute() else HERE / url
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
        if text.lstrip().startswith("{"):
            data = json.loads(text)
            if _find_launch(data):
                return [{"__launch__": data}], ["__launch__"]
        rd = csv.DictReader(io.StringIO(text))
        rows = [{k: (v if v != "" else None) for k, v in row.items()} for row in rd]
        return rows, rd.fieldnames or []
    r = requests.get(url, timeout=60, headers={"User-Agent": "aero-inflation-bot"})
    r.raise_for_status()
    text = r.text.lstrip("\ufeff")
    if text.lstrip().startswith(("{", "[")):  # JSON instead of CSV
        data = r.json()
        if isinstance(data, dict) and _find_launch(data):
            return [{"__launch__": data}], ["__launch__"]
        if isinstance(data, dict) and url.rstrip("/").endswith("aero-analytics"):
            return _csv_rows(url.rstrip("/")[: -len("aero-analytics")] + "aero-launch")
        rows = data if isinstance(data, list) else (data.get("rows") or data.get("data") or [])
        return rows, list(rows[0].keys()) if rows else []
    rd = csv.DictReader(io.StringIO(text))
    rows = [{k: (v if v != "" else None) for k, v in row.items()} for row in rd]
    return rows, rd.fieldnames or []


def _find_launch(node, depth: int = 0):
    """Find the block with days + values.emissions anywhere in the JSON."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        v = node.get("values")
        if "days" in node and isinstance(v, dict) and isinstance(v.get("emissions"), list):
            return node
        for k in ("launch",) + tuple(node.keys()):
            if k in node:
                hit = _find_launch(node[k], depth + 1)
                if hit:
                    return hit
    return None


def _parse_launch_json(data: dict) -> dict | None:
    """Dromos Kitchen JSON: a block with days + values.{emissions,voting_rewards,...}."""
    L = _find_launch(data)
    if not L:
        return None
    days = [datetime.fromisoformat(d).date() for d in L["days"]]
    vals = L["values"]
    today = datetime.now(timezone.utc).date()
    idx = [i for i, d in enumerate(days) if d < today][-7:] or list(range(len(days)))[-7:]
    avg = lambda k: (sum(vals[k][i] for i in idx) / len(idx)) if vals.get(k) else None
    last = idx[-1]
    chains = {c: v["emissions"][last] for c, v in (L.get("byChain") or {}).items() if v.get("emissions")}
    fees_by_chain = {c: sum(v["fees"][i] for i in idx) for c, v in (L.get("byChain") or {}).items() if v.get("fees")}
    return {
        "source": "dromos launch json", "latest": days[last], "n_days": len(idx),
        "usd_per_day": avg("emissions"), "aero_per_day": None,
        "rewards_per_day": avg("voting_rewards"), "fees_per_day": avg("fees"),
        "chains": chains, "fees_by_chain": fees_by_chain,
    }


def fetch_dune_emissions(refresh: bool = False) -> dict | None:
    """Daily AERO emissions: from EMISSIONS_CSV_URL (free, e.g. the Dromos dashboard's
    CSV link) or the Dune API if DUNE_API_KEY is set.
    Returns trailing-7-full-day totals (USD and/or AERO), latest day, columns."""
    key = os.getenv("DUNE_API_KEY")
    if not (EMISSIONS_CSV_URL or key):
        return None
    try:
        rows, cols = _csv_rows(EMISSIONS_CSV_URL) if EMISSIONS_CSV_URL else _dune_rows(key, refresh)
        if cols == ["__launch__"]:
            return _parse_launch_json(rows[0]["__launch__"]) or {"error": "unrecognized JSON"}
        if not rows:
            return {"error": "no rows", "cols": cols}
        cols = cols or list(rows[0].keys())
        dcol = (_pick(cols, DUNE_DATE_COL, ("day",)) or _pick(cols, "", ("date",))
                or _pick(cols, "", ("time",)) or _pick(cols, "", ("epoch",)))
        bad = ("fee", "revenue", "tvl", "volume", "legacy", "velo", "allocation")
        ucol = (_pick(cols, DUNE_USD_COL, ("usd",), prefer=("aero", "emission"), avoid=bad)
                or (None if DUNE_USD_COL else _pick(cols, "", ("aero",), avoid=bad + ("price",))))
        acol = _pick(cols, DUNE_AERO_COL, ("aero",), prefer=("emission", "amount", "token"), avoid=bad + ("usd", "price", "$"))
        if not acol:
            acol = _pick(cols, "", ("emission",), avoid=bad + ("usd", "price"))
        if ucol and acol == ucol:
            acol = None
        if not dcol or not (ucol or acol):
            return {"error": "could not identify columns", "cols": cols}
        by_day: dict = {}
        for r in rows:
            if str(r.get("partial_day", "")).lower() == "true":
                continue
            d = _to_date(r.get(dcol))
            if d is None:
                continue
            k = d.date()
            agg = by_day.setdefault(k, [0.0, 0.0])
            if ucol and r.get(ucol) not in (None, ""):
                agg[0] += float(str(r[ucol]).replace(",", ""))
            if acol and r.get(acol) not in (None, ""):
                agg[1] += float(str(r[acol]).replace(",", ""))
        days = sorted(d for d in by_day if by_day[d][0] > 0 or by_day[d][1] > 0)
        if not days:
            return {"error": "no dated rows", "cols": cols}
        today = datetime.now(timezone.utc).date()
        full = [d for d in days if d < today][-7:]  # skip today's partial day
        if not full:
            full = days[-7:]
        n = len(full)
        chain_cols = [c for c in cols if c.lower().startswith("aero_") and c != ucol and c != acol]
        chains = {}
        if chain_cols:
            last = full[-1]
            for r in rows:
                d = _to_date(r.get(dcol))
                if d and d.date() == last and str(r.get("partial_day", "")).lower() != "true":
                    for c in chain_cols:
                        if r.get(c) not in (None, ""):
                            chains[c[5:]] = chains.get(c[5:], 0.0) + float(r[c])
        return {
            "chains": chains,
            "cols": cols, "date_col": dcol, "usd_col": ucol, "aero_col": acol,
            "latest": days[-1], "n_days": n,
            "usd_per_day": sum(by_day[d][0] for d in full) / n if ucol else None,
            "aero_per_day": sum(by_day[d][1] for d in full) / n if acol else None,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------- history
FIELDS = ["timestamp", "base_supply", "xaero_supply", "total_supply", "foundation_balance", "excluded", "price", "rev_30d", "migrated"]


def load_history() -> list[Snapshot]:
    if not HISTORY.exists():
        return []
    out = []
    with HISTORY.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            fb = row.get("foundation_balance")
            out.append(
                Snapshot(
                    datetime.fromisoformat(row["timestamp"]),
                    float(row["base_supply"]),
                    float(row["xaero_supply"]),
                    float(fb) if fb not in (None, "") else None,
                    float(row.get("excluded") or 0),
                    float(row["price"]) if row.get("price") else None,
                    float(row["rev_30d"]) if row.get("rev_30d") else None,
                    float(row.get("migrated") or 0),
                )
            )
    return out


def _upgrade_header() -> None:
    """Rewrite an older CSV with the current columns so new fields aren't lost."""
    with HISTORY.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        header = rows[0].keys() if rows else []
    if rows and list(header) != FIELDS:
        with HISTORY.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: (r.get(k) or "") for k in FIELDS})


def append_history(s: Snapshot) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    if HISTORY.exists():
        _upgrade_header()
    new = not HISTORY.exists()
    with HISTORY.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(
            {
                "timestamp": s.ts.isoformat(),
                "base_supply": f"{s.base_supply:.4f}",
                "xaero_supply": f"{s.xaero_supply:.4f}",
                "total_supply": f"{s.total:.4f}",
                "foundation_balance": "" if s.foundation_balance is None else f"{s.foundation_balance:.4f}",
                "excluded": f"{s.excluded:.4f}",
                "price": "" if s.price is None else f"{s.price:.6f}",
                "rev_30d": "" if s.rev_30d is None else f"{s.rev_30d:.2f}",
                "migrated": f"{s.migrated:.4f}",
            }
        )


# ---------------------------------------------------------------- math
def annualized(prev: Snapshot, cur: Snapshot) -> float | None:
    days = (cur.ts - prev.ts).total_seconds() / 86400
    if days < 0.5 or prev.econ <= 0:
        return None
    return (cur.econ / prev.econ) ** (365.25 / days) - 1


def apply_spike_guard(history: list[Snapshot], cur: Snapshot) -> float:
    """Carry forward exclusions; if this run looks like a one-off mint, exclude the
    excess over a normal emission rate. Returns the amount newly excluded."""
    if not history:
        return 0.0
    prev = history[-1]
    cur.excluded = prev.excluded
    days = (cur.ts - prev.ts).total_seconds() / 86400
    if days < 0.5:
        return 0.0
    raw = annualized(prev, cur)
    if raw is None or raw < SPIKE:
        return 0.0
    m4 = nearest_before(history, prev, 28)
    base_rate = annualized(m4, prev) if m4 else None
    if base_rate is None or base_rate < 0 or base_rate >= SPIKE:
        base_rate = WATCH
    expected = prev.econ * ((1 + base_rate) ** (days / 365.25) - 1)
    excess = max(0.0, (cur.total - prev.total) - expected)
    cur.excluded += excess
    return excess


def nearest_before(history: list[Snapshot], cur: Snapshot, days_back: float) -> Snapshot | None:
    """Oldest snapshot that is at least ~days_back old, closest to that age."""
    target = days_back * 86400
    best = None
    for s in history:
        age = (cur.ts - s.ts).total_seconds()
        if age >= target * 0.8 and (best is None or age < (cur.ts - best.ts).total_seconds()):
            best = s
    return best


def verdict(rate: float | None) -> str:
    if rate is None:
        return "⏳ not enough data yet"
    if rate >= BROKEN:
        return "🔴 at/above old Aerodrome rate — thesis item 3 FAILING"
    if rate >= WATCH:
        return "🟡 above the ~8% projection — watch"
    return "🟢 below 8% — AER Engine holding"


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"


def build_report(history: list[Snapshot], cur: Snapshot, new_excluded: float = 0.0, em: dict | None = None) -> str:
    prev = history[-1] if history else None
    wk = nearest_before(history, cur, 7)
    m4 = nearest_before(history, cur, 28)
    first = history[0] if history else None

    r_last = annualized(prev, cur) if prev else None
    r_wk = annualized(wk, cur) if wk else None
    r_4w = annualized(m4, cur) if m4 else None
    r_all = annualized(first, cur) if first else None

    headline = r_4w if r_4w is not None else r_wk if r_wk is not None else r_all

    # A real emitting token never sits perfectly flat for days. If it does, this
    # contract isn't where emissions land, so don't report a fake 0%.
    # CSV stores 4 decimals, RPC returns 18 -> compare with a tolerance, not ==
    frozen = bool(first) and abs(cur.adj_total - first.adj_total) < 1.0 and (cur.ts - first.ts).total_seconds() > 2 * 86400
    if frozen:
        headline = None
    em_infl = emissions_inflation(em, cur)

    lines = [
        "✈️ *AERO weekly inflation*",
        f"`{cur.ts:%Y-%m-%d %H:%M} UTC`",
        "",
        f"Total supply: `{cur.total:,.0f}`",
        f"  Base AERO: `{cur.base_supply:,.0f}`",
        f"  xAERO (receipt tokens): `{cur.xaero_supply:,.0f}`"
        + (f" ({', '.join(f'{k} {v:,.0f}' for k, v in XAERO_BY_CHAIN.items() if v >= 1)})" if XAERO_BY_CHAIN else ""),
    ]
    if MIGRATION_MODE:
        lines.append(f"  Migrated in (AERO + VELO×{VELO_RATIO}): `{cur.migrated:,.0f}` "
                     f"({cur.migrated / ECON_BASE:.0%} of ~{ECON_BASE / 1e9:.2f}B)")
        lines.append(f"  New AERO emitted since launch: `{cur.adj_total:,.0f}`")
        lines.append(f"  Economic supply: `{cur.econ:,.0f}`")
    if prev:
        lines.append(f"Minted since last (ex-migration): `{cur.adj_total - prev.adj_total:+,.0f}`")
    if new_excluded > 0:
        lines.append(f"⚠️ One-off mint suspected (VELO migration?): `{new_excluded:,.0f}` excluded from inflation")
    if cur.excluded > 0:
        lines.append(f"Total one-off mints excluded so far: `{cur.excluded:,.0f}`")
    lines += [
        "",
        "*Annualized realized inflation*",
        f"  Since last run: `{pct(r_last)}`",
        f"  1-week: `{pct(r_wk)}`",
        f"  4-week: `{pct(r_4w)}`",
        f"  Since first snapshot: `{pct(r_all)}`",
        "",
        (f"Verdict (emissions-based {pct(em_infl)}): {verdict(em_infl)}" if em_infl is not None
         else "Verdict: ⚠️ supply frozen since first snapshot — this contract is NOT "
              "capturing emissions; inflation unknown" if frozen else f"Verdict: {verdict(headline)}"),
    ]
    if frozen:
        lines.append("_On-chain supply is frozen (new rewards not minted on this contract); "
                     "use the Emissions section below._")
    lines += valuation_lines(cur, None if frozen else headline)
    lines += emissions_lines(em, cur)
    if cur.foundation_balance is not None:
        share = cur.foundation_balance / cur.econ if cur.econ else 0
        lines.append(f"Foundation wallet: `{cur.foundation_balance:,.0f}` ({share * 100:.2f}% of supply)")
    lines += ["", "_Mint-on-claim: early weeks read low until LP claims catch up._"]
    return "\n".join(lines)


def valuation_lines(cur: Snapshot, realized: float | None) -> list[str]:
    if cur.price is None:
        return ["", "_Price unavailable — valuation section skipped._"]
    fdv = cur.econ * cur.price
    out = ["", "*Valuation vs dilution*", f"  Price: `${cur.price:,.4f}`  FDV: `${fdv / 1e9:,.2f}B`"]
    if not cur.rev_30d:
        out.append("  _Holder revenue unavailable — check DEFILLAMA_SLUGS._")
        return out
    rev_yr = cur.rev_30d * 365 / 30
    multiple = fdv / rev_yr
    implied = CAP_MULTIPLE * rev_yr / fdv
    staker = rev_yr / (STAKED_SHARE * fdv)
    out += [
        f"  Holder rev (median-day run-rate): `${rev_yr / 1e6:,.0f}M/yr`",
        f"  FDV / revenue: `{multiple:,.1f}x`",
        f"  Implied issuance at {CAP_MULTIPLE}x cap: `{pct(implied)}`",
        f"  Staker gross yield (~{STAKED_SHARE:.0%} staked): `{pct(staker)}`",
    ]
    info = LAST_REV_INFO
    if info.get("spikes"):
        out.append(f"  ⚠️ Lump payment in window: raw 30d `${info['raw30'] / 1e6:,.1f}M` vs run-rate `${info['median30'] / 1e6:,.1f}M`")
    if realized is not None:
        out.append(f"  Staker net of realized inflation: `{pct(staker - realized)}`")
        gap = realized - implied
        if gap > 0.03:
            out.append("  ⚠️ Realized inflation well above implied — protocol overpaying")
        elif implied >= BROKEN:
            out.append("  ⚠️ Implied issuance high because price is low vs revenue (reflexive)")
    return out


def emissions_inflation(em: dict | None, cur: Snapshot) -> float | None:
    if not em or em.get("error"):
        return None
    if em.get("aero_per_day"):
        return em["aero_per_day"] * 365 / cur.econ
    if em.get("usd_per_day") and cur.price:
        return em["usd_per_day"] * 365 / (cur.econ * cur.price)
    return None


def emissions_lines(em: dict | None, cur: Snapshot) -> list[str]:
    if em is None:
        return []
    if em.get("error"):
        return ["", f"_Dune emissions unavailable: {em['error']}_"]
    out = ["", "*Emissions (Aero v3, daily data)*",
           f"  Data through: `{em['latest']}`  ({em['n_days']}-day avg)"]
    infl = None
    if em.get("aero_per_day"):
        out.append(f"  AERO emitted/day: `{em['aero_per_day']:,.0f}`")
        infl = em["aero_per_day"] * 365 / cur.econ
    usd = em.get("usd_per_day")
    if usd is None and em.get("aero_per_day") and cur.price:
        usd = em["aero_per_day"] * cur.price
    if usd:
        out.append(f"  USD emitted/day: `${usd:,.0f}`")
        if infl is None and cur.price:
            infl = usd * 365 / (cur.econ * cur.price)
    if infl is not None:
        out.append(f"  Emissions-based inflation: `{pct(infl)}`  {verdict(infl)[:1]}")
    rew = em.get("rewards_per_day") or (cur.rev_30d / 30 if cur.rev_30d else None)
    if em.get("rewards_per_day"):
        out.append(f"  Staker rewards/day (Aero v3): `${em['rewards_per_day']:,.0f}`")
    if usd and rew:
        ratio = rew / usd
        tag = "🔴 stakers losing" if ratio < 0.5 else "🟡 stakers positive" if ratio < 0.83 else "🟢 caps binding" if ratio < 1 else "🟢 revenue covers dilution"
        out.append(f"  Rewards / emissions: `{ratio:.2f}x`  {tag}")
        if infl is not None:
            out.append(f"  Staker net (gross − emissions infl.): `{pct((rew * 365) / (STAKED_SHARE * cur.econ * cur.price) - infl)}`")
    ch = em.get("chains") or {}
    tot = sum(ch.values())
    if tot > 0:
        base = ch.get("base", 0) / tot
        others = sorted(((k, v / tot) for k, v in ch.items() if k != "base"), key=lambda x: -x[1])
        out.append(f"  Emissions on Base: `{base:.0%}`  other chains: `{1 - base:.0%}`")
        out.append("  " + ", ".join(f"{k} {v:.1%}" for k, v in others[:4]))
    stale = (datetime.now(timezone.utc).date() - em["latest"]).days
    if stale > 3:
        out.append(f"  ⚠️ Dune data is {stale} days old")
    return out


# ---------------------------------------------------------------- telegram
def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping send.")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")
    print("[telegram] sent.")


# ---------------------------------------------------------------- modes
def check() -> None:
    base_rpc = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    aero = os.getenv("AERO_TOKEN_BASE")
    print(f"Base RPC: {base_rpc}")
    print(f"AERO_TOKEN_BASE: {aero}")
    sym = erc20_symbol(base_rpc, aero)
    sup = erc20_total_supply(base_rpc, aero)
    print(f"  symbol={sym!r}  totalSupply={sup:,.0f}")
    if sym.upper() != "AERO":
        print("  ⚠️  symbol is not AERO — wrong address?")
    for chain in OTHER_CHAINS:
        rpc, tok = os.getenv(f"{chain}_RPC_URL"), os.getenv(f"XAERO_{chain}")
        if rpc and tok:
            print(f"{chain}: symbol={erc20_symbol(rpc, tok)!r} supply={erc20_total_supply(rpc, tok):,.0f}")
        else:
            print(f"{chain}: not configured (skipped)")


def self_test() -> None:
    from datetime import timedelta

    t0 = datetime(2026, 10, 22, tzinfo=timezone.utc)
    base = 2_100_000_000.0
    hist = []
    # 5 weeks at ~6.6% annual, then a jump week at ~15%
    rates = [0.066, 0.066, 0.07, 0.072, 0.15]
    supply = base
    hist.append(Snapshot(t0, supply, 0, None))
    for i, r in enumerate(rates, 1):
        supply *= (1 + r) ** (7 / 365.25)
        hist.append(Snapshot(t0 + timedelta(days=7 * i), supply * 0.97, supply * 0.03, 50_000_000, price=0.84, rev_30d=14_900_000))
    cur = hist[-1]
    rep = build_report(hist[:-1], cur)
    print(rep)
    r_wk = annualized(hist[-2], cur)
    assert abs(r_wk - 0.15) < 1e-6, r_wk

    # VELO migration: +116M lands in one week on top of ~7% emissions
    print("\n--- migration week ---")
    nxt = Snapshot(cur.ts + timedelta(days=7), cur.base_supply * 1.0013 + 116_000_000, cur.xaero_supply, 50_000_000, price=0.84, rev_30d=14_900_000)
    ex = apply_spike_guard(hist, nxt)
    print(build_report(hist, nxt, ex))
    assert 110_000_000 < ex < 117_000_000, ex
    assert annualized(hist[-1], nxt) < SPIKE, annualized(hist[-1], nxt)
    assert verdict(0.05).startswith("🟢") and verdict(0.09).startswith("🟡") and verdict(0.13).startswith("🔴")
    print("\nself-test OK")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--check-dune", action="store_true", help="show Dune columns and parsed emissions")
    ap.add_argument("--min-days", type=float, default=0,
                    help="skip quietly if the last snapshot is younger than this (scheduled runs)")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if a.check:
        return check()
    if a.check_dune:
        em = fetch_dune_emissions(refresh=False)
        print(json.dumps(em, default=str, indent=2))
        if em and not em.get("error") and not EMISSIONS_CSV_URL and (datetime.now(timezone.utc).date() - em["latest"]).days > 2:
            print("Cached results stale; executing query (uses Dune credits)...")
            print(json.dumps(fetch_dune_emissions(refresh=True), default=str, indent=2))
        return

    history = load_history()
    if a.min_days and history:
        age = (datetime.now(timezone.utc) - history[-1].ts).total_seconds() / 86400
        if age < a.min_days:
            print(f"Last snapshot is {age:.1f} days old (< {a.min_days}); nothing to do.")
            return
    cur = take_snapshot()
    new_ex = apply_spike_guard(history, cur)
    em = fetch_dune_emissions(refresh=False)
    if em and not em.get("error") and not EMISSIONS_CSV_URL and (datetime.now(timezone.utc).date() - em["latest"]).days > 2:
        em = fetch_dune_emissions(refresh=True) or em
    report = build_report(history, cur, new_ex, em)
    print(report)
    if a.dry_run:
        return
    append_history(cur)
    send_telegram(report)


if __name__ == "__main__":
    main()
