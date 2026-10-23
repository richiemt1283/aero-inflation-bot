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

OTHER_CHAINS = ["ETHEREUM", "OPTIMISM", "ARBITRUM", "INK", "ROBINHOOD", "ARC"]


@dataclass
class Snapshot:
    ts: datetime
    base_supply: float
    xaero_supply: float
    foundation_balance: float | None
    excluded: float = 0.0  # cumulative one-off mints removed (e.g. VELO migration)
    price: float | None = None      # AERO/USD
    rev_30d: float | None = None    # holder revenue, last 30 days, USD

    @property
    def total(self) -> float:
        return self.base_supply + self.xaero_supply

    @property
    def adj_total(self) -> float:
        """Supply minus one-off mints: what inflation is measured on."""
        return self.total - self.excluded


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
        rpc = os.getenv(f"{chain}_RPC_URL")
        token = os.getenv(f"XAERO_{chain}")
        if rpc and token:
            xaero_total += erc20_total_supply(rpc, token)

    foundation = os.getenv("AERO_FOUNDATION_ADDRESS")
    fbal = erc20_balance(base_rpc, aero, foundation) if foundation else None

    return Snapshot(datetime.now(timezone.utc), base_supply, xaero_total, fbal,
                    price=fetch_price(), rev_30d=fetch_holder_rev_30d())


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
    for slug in DEFILLAMA_SLUGS:
        try:
            r = requests.get(
                f"https://api.llama.fi/summary/fees/{slug}",
                params={"dataType": "dailyHoldersRevenue"},
                timeout=30,
            )
            if r.status_code != 200:
                continue
            v = r.json().get("total30d")
            if v:
                return float(v)
        except Exception as e:
            print(f"[defillama:{slug}] {e}")
    print("[revenue] unavailable from DefiLlama")
    return None


# ---------------------------------------------------------------- history
FIELDS = ["timestamp", "base_supply", "xaero_supply", "total_supply", "foundation_balance", "excluded", "price", "rev_30d"]


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
            }
        )


# ---------------------------------------------------------------- math
def annualized(prev: Snapshot, cur: Snapshot) -> float | None:
    days = (cur.ts - prev.ts).total_seconds() / 86400
    if days < 0.5 or prev.adj_total <= 0:
        return None
    return (cur.adj_total / prev.adj_total) ** (365.25 / days) - 1


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
    expected = prev.adj_total * ((1 + base_rate) ** (days / 365.25) - 1)
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


def build_report(history: list[Snapshot], cur: Snapshot, new_excluded: float = 0.0) -> str:
    prev = history[-1] if history else None
    wk = nearest_before(history, cur, 7)
    m4 = nearest_before(history, cur, 28)
    first = history[0] if history else None

    r_last = annualized(prev, cur) if prev else None
    r_wk = annualized(wk, cur) if wk else None
    r_4w = annualized(m4, cur) if m4 else None
    r_all = annualized(first, cur) if first else None

    headline = r_4w if r_4w is not None else r_wk if r_wk is not None else r_all

    lines = [
        "✈️ *AERO weekly inflation*",
        f"`{cur.ts:%Y-%m-%d %H:%M} UTC`",
        "",
        f"Total supply: `{cur.total:,.0f}`",
        f"  Base AERO: `{cur.base_supply:,.0f}`",
        f"  xAERO (other chains): `{cur.xaero_supply:,.0f}`",
    ]
    if prev:
        lines.append(f"Minted since last: `{cur.total - prev.total:+,.0f}`")
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
        f"Verdict: {verdict(headline)}",
    ]
    lines += valuation_lines(cur, headline)
    if cur.foundation_balance is not None:
        share = cur.foundation_balance / cur.total if cur.total else 0
        lines.append(f"Foundation wallet: `{cur.foundation_balance:,.0f}` ({share * 100:.2f}% of supply)")
    lines += ["", "_Mint-on-claim: early weeks read low until LP claims catch up._"]
    return "\n".join(lines)


def valuation_lines(cur: Snapshot, realized: float | None) -> list[str]:
    if cur.price is None:
        return ["", "_Price unavailable — valuation section skipped._"]
    fdv = cur.adj_total * cur.price
    out = ["", "*Valuation vs dilution*", f"  Price: `${cur.price:,.4f}`  FDV: `${fdv / 1e9:,.2f}B`"]
    if not cur.rev_30d:
        out.append("  _Holder revenue unavailable — check DEFILLAMA_SLUGS._")
        return out
    rev_yr = cur.rev_30d * 365 / 30
    multiple = fdv / rev_yr
    implied = CAP_MULTIPLE * rev_yr / fdv
    staker = rev_yr / (STAKED_SHARE * fdv)
    out += [
        f"  Holder rev (30d, annualized): `${rev_yr / 1e6:,.0f}M`",
        f"  FDV / revenue: `{multiple:,.1f}x`",
        f"  Implied issuance at {CAP_MULTIPLE}x cap: `{pct(implied)}`",
        f"  Staker gross yield (~{STAKED_SHARE:.0%} staked): `{pct(staker)}`",
    ]
    if realized is not None:
        out.append(f"  Staker net of realized inflation: `{pct(staker - realized)}`")
        gap = realized - implied
        if gap > 0.03:
            out.append("  ⚠️ Realized inflation well above implied — protocol overpaying")
        elif implied >= BROKEN:
            out.append("  ⚠️ Implied issuance high because price is low vs revenue (reflexive)")
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
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if a.check:
        return check()

    history = load_history()
    cur = take_snapshot()
    new_ex = apply_spike_guard(history, cur)
    report = build_report(history, cur, new_ex)
    print(report)
    if a.dry_run:
        return
    append_history(cur)
    send_telegram(report)


if __name__ == "__main__":
    main()
