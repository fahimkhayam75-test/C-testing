#!/usr/bin/env python3
"""
Solana memecoin PAPER bot v2. No wallet, no keys, no real money. Python 3.8+, stdlib only.

  python3 paper_bot.py once     one scan then exit
  python3 paper_bot.py          loop forever, scanning every 5 min
  python3 paper_bot.py report   print results per strategy

Three strategies (SAFE / BALANCED / MOMENTUM) trade the SAME live tokens with
separate pretend bankrolls, so you learn which one actually works.
Paper results are optimistic. Don't trust any strategy before ~30 closed trades.
"""
import csv, json, os, sys, time, urllib.request
from datetime import datetime, timezone

START_CASH = 100.0
COMMON = dict(
    MAX_OPEN=4,          # max positions per strategy
    SIZE_PCT=0.05,       # 5% of equity per trade -> with a -20% stop, risk is ~1% per trade
    SLIP=0.03,           # 3% slippage each way (conservative)
    FEE_USD=0.03,        # network + priority fee per swap
    DAILY_LOSS_CAP=6.0,  # stop opening trades after losing this many $ in a day
    STREAK_PAUSE=4,      # pause after this many losses in a row
    PAUSE_MIN=180,
    MAX_HOLD_MIN=480,    # hard time exit
    STALE_MIN=120,       # exit "dead money" that hasn't moved
    DEAD_RET=0.10,
    LIQ_PULL=0.5,        # exit instantly if liquidity falls below 50% of entry
    TP2=3.0,             # sell everything at +300%
)
VARIANTS = {
    # SAFE: older, deeper, calmer tokens. Strictest safety gate. Fewer trades.
    "SAFE": dict(MIN_LIQ=60_000, MIN_VOL=40_000, MIN_AGE=120, MAX_AGE=1440, MIN_BUYS=100,
                 MAX_SB=1.0, H1_MIN=0, H1_MAX=60, M5_MIN=-2, SL=-0.20, TP1=0.40,
                 TP1_FRAC=0.5, TRAIL=0.20, RC="strict"),
    # BALANCED: buy healthy uptrends that are not already parabolic.
    "BALANCED": dict(MIN_LIQ=30_000, MIN_VOL=20_000, MIN_AGE=45, MAX_AGE=720, MIN_BUYS=60,
                     MAX_SB=1.1, H1_MIN=5, H1_MAX=120, M5_MIN=0, SL=-0.20, TP1=0.40,
                     TP1_FRAC=0.5, TRAIL=0.20, RC="core"),
    # MOMENTUM: faster, riskier, bigger upside.
    "MOMENTUM": dict(MIN_LIQ=20_000, MIN_VOL=30_000, MIN_AGE=30, MAX_AGE=360, MIN_BUYS=60,
                     MAX_SB=1.2, H1_MIN=20, H1_MAX=200, M5_MIN=3, SL=-0.20, TP1=0.50,
                     TP1_FRAC=0.5, TRAIL=0.25, RC="core"),
}
DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
STATE_FILE, LOG_FILE = "state_v3.json", "trades_v3.csv"
OFFSET = 0  # test hook: shifts the clock


def tnow():
    return time.time() + OFFSET


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/2.0",
                                               "Accept": "application/json;version=20230302"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def blank_variant():
    return dict(cash=START_CASH, open={}, closed=[], traded=[], day="", day_pnl=0.0,
                streak=0, pause_until=0)


def load():
    s = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    s.setdefault("v", {})
    for n in VARIANTS:
        s["v"].setdefault(n, blank_variant())
    s.setdefault("cool", {})
    s.setdefault("rc", {})
    return s


def save(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


def log(row):
    new = not os.path.exists(LOG_FILE)
    row = list(row) + [""] * (10 - len(row))
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "strategy", "action", "symbol", "address", "price", "usd", "pnl_usd",
                        "reason", "entry_info"])
        w.writerow(row)


# ---------- data ----------
def discover():
    addrs = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            addrs += [t["tokenAddress"] for t in get(DEX + path) if t.get("chainId") == "solana"]
        except Exception as e:
            print("dexscreener feed error:", e)
    for path in ("/networks/solana/trending_pools", "/networks/solana/new_pools"):
        try:
            for pool in get(GECKO + path)["data"]:
                addrs.append(pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1])
        except Exception as e:
            print("geckoterminal feed error:", e)
    return list(dict.fromkeys(addrs))


def pairs_for(addrs):
    best = {}
    for i in range(0, len(addrs), 30):
        try:
            for p in get(DEX + "/tokens/v1/solana/" + ",".join(addrs[i:i + 30])):
                a = p["baseToken"]["address"]
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if a not in best or liq > ((best[a].get("liquidity") or {}).get("usd") or 0):
                    best[a] = p
        except Exception as e:
            print("pair error:", e)
    return best


def rug_facts(addr, s):
    """RugCheck facts, cached 6h. Returns None if unavailable (callers must fail closed)."""
    c = s["rc"].get(addr)
    if c and tnow() - c["t"] < 6 * 3600:
        return c
    try:
        r = get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report")
        c = dict(t=tnow(), rugged=bool(r.get("rugged")), mint=bool(r.get("mintAuthority")),
                 freeze=bool(r.get("freezeAuthority")),
                 danger=[str(k.get("name", "")).lower() for k in (r.get("risks") or [])
                         if str(k.get("level", "")).lower() == "danger"])
        s["rc"][addr] = c
        return c
    except Exception:
        return None


CORE_BLOCK = ("single holder", "lp", "liquidity", "creator", "honeypot", "copycat", "rug", "freeze", "mint")


def rug_ok(addr, mode, s):
    c = rug_facts(addr, s)
    if c is None:
        return False, "safety check unavailable"
    if c["rugged"]: return False, "rugged"
    if c["mint"]: return False, "mint authority on"
    if c["freeze"]: return False, "freeze authority on"
    if mode == "strict" and c["danger"]: return False, "danger: " + c["danger"][0]
    for d in c["danger"]:
        if any(k in d for k in CORE_BLOCK):
            return False, "danger: " + d
    return True, "clean"


# ---------- strategy ----------
def metrics(p):
    tx = (p.get("txns") or {}).get("h1") or {}
    pc = p.get("priceChange") or {}
    created = p.get("pairCreatedAt")
    return dict(
        liq=(p.get("liquidity") or {}).get("usd") or 0,
        vol=(p.get("volume") or {}).get("h1") or 0,
        buys=tx.get("buys", 0), sells=tx.get("sells", 0),
        h1=pc.get("h1") or 0, m5=pc.get("m5") or 0,
        age=(tnow() * 1000 - created) / 60000 if created else None)


def screen(p, V):
    """Cheap market checks. Returns (ok, reason)."""
    m = metrics(p)
    if not p.get("priceUsd") or m["age"] is None: return False, "no data"
    if m["age"] < V["MIN_AGE"]: return False, "too young"
    if m["age"] > V["MAX_AGE"]: return False, "too old"
    if m["liq"] < V["MIN_LIQ"]: return False, "low liquidity"
    if m["vol"] < V["MIN_VOL"]: return False, "low volume"
    if m["buys"] < V["MIN_BUYS"]: return False, "few buys"
    if m["sells"] > m["buys"] * V["MAX_SB"]: return False, "sell pressure"
    if m["h1"] < V["H1_MIN"]: return False, "no uptrend"
    if m["h1"] > V["H1_MAX"]: return False, "already pumped"
    if m["m5"] < V["M5_MIN"]: return False, "momentum fading"
    return True, "ok"


def sell(s, name, addr, frac, price, reason, extra_slip=0.0):
    V, pos = s["v"][name], s["v"][name]["open"][addr]
    qty = pos["qty"] * frac
    cost = pos["cost"] * frac
    proceeds = max(0.0, qty * price * (1 - COMMON["SLIP"] - extra_slip) - COMMON["FEE_USD"])
    pnl = proceeds - cost
    V["cash"] += proceeds
    V["day_pnl"] += pnl
    pos["qty"] -= qty
    pos["cost"] -= cost
    pos["realized"] += pnl
    log([datetime.fromtimestamp(tnow(), timezone.utc).isoformat(), name, "SELL", pos["sym"], addr,
         price, round(proceeds, 2), round(pnl, 2), reason])
    print(f"[{name}] SELL {int(frac * 100)}% {pos['sym']} pnl ${pnl:+.2f} ({reason})")
    if frac >= 0.999:
        total = pos["realized"]
        V["closed"].append(dict(sym=pos["sym"], pnl=round(total, 4), t=tnow()))
        V["open"].pop(addr)
        V["streak"] = V["streak"] + 1 if total < 0 else 0
        if V["streak"] >= COMMON["STREAK_PAUSE"]:
            V["pause_until"] = tnow() + COMMON["PAUSE_MIN"] * 60
            V["streak"] = 0
            print(f"[{name}] loss streak, pausing new entries {COMMON['PAUSE_MIN']} min")


def manage(s, name, px):
    P, V = VARIANTS[name], s["v"][name]
    for addr in list(V["open"]):
        pos = V["open"][addr]
        p = px.get(addr)
        held = (tnow() - pos["t"]) / 60
        if not p or not p.get("priceUsd"):
            pos["miss"] = pos.get("miss", 0) + 1
            if pos["miss"] >= 3:
                sell(s, name, addr, 1.0, 0.0, "token vanished")
            continue
        pos["miss"] = 0
        price = float(p["priceUsd"])
        liq = (p.get("liquidity") or {}).get("usd") or 0
        pos["peak"] = max(pos["peak"], price)
        ret = price / pos["entry"] - 1
        if liq < pos["liq0"] * COMMON["LIQ_PULL"]:
            sell(s, name, addr, 1.0, price, "liquidity pulled", extra_slip=0.10)
        elif ret >= COMMON["TP2"]:
            sell(s, name, addr, 1.0, price, "take profit 2")
        elif pos["stage"] == 0 and ret >= P["TP1"]:
            sell(s, name, addr, P["TP1_FRAC"], price, "take profit 1")
            pos["stage"] = 1
        elif pos["stage"] == 0 and price <= pos["entry"] * (1 + P["SL"]):
            sell(s, name, addr, 1.0, price, "stop loss")
        elif pos["stage"] == 1 and price <= max(pos["entry"], pos["peak"] * (1 - P["TRAIL"])):
            sell(s, name, addr, 1.0, price, "trailing stop")
        elif pos["stage"] == 0 and held >= COMMON["STALE_MIN"] and ret < COMMON["DEAD_RET"]:
            sell(s, name, addr, 1.0, price, "dead money")
        elif held >= COMMON["MAX_HOLD_MIN"]:
            sell(s, name, addr, 1.0, price, "time exit")


def can_open(s, name):
    V = s["v"][name]
    today = datetime.fromtimestamp(tnow(), timezone.utc).strftime("%Y-%m-%d")
    if V["day"] != today:
        V["day"], V["day_pnl"] = today, 0.0
    return (len(V["open"]) < COMMON["MAX_OPEN"] and tnow() >= V["pause_until"]
            and V["day_pnl"] > -COMMON["DAILY_LOSS_CAP"])


def equity(V):
    return V["cash"] + sum(p["cost"] for p in V["open"].values())


def buy(s, name, addr, p):
    V = s["v"][name]
    size = min(V["cash"] - COMMON["FEE_USD"], equity(V) * COMMON["SIZE_PCT"])
    if size < 1:
        return
    price = float(p["priceUsd"]) * (1 + COMMON["SLIP"])
    V["cash"] -= size + COMMON["FEE_USD"]
    V["open"][addr] = dict(sym=p["baseToken"]["symbol"], entry=price, peak=price, qty=size / price,
                           cost=size + COMMON["FEE_USD"], stage=0, t=tnow(), realized=0.0,
                           liq0=metrics(p)["liq"])
    V["traded"] = (V["traded"] + [addr])[-500:]
    log([datetime.fromtimestamp(tnow(), timezone.utc).isoformat(), name, "BUY", p["baseToken"]["symbol"],
         addr, price, round(size, 2), "", "passed filters",
         "liq={liq:.0f};vol_h1={vol:.0f};age_min={age:.0f};buys={buys};sells={sells};h1={h1};m5={m5}".format(**metrics(p))])
    print(f"[{name}] BUY {p['baseToken']['symbol']} ${size:.2f} @ {price:.8f}")


def step(s):
    held = {a for V in s["v"].values() for a in V["open"]}
    if held:
        px = pairs_for(list(held))
        for name in VARIANTS:
            manage(s, name, px)
    s["cool"] = {a: t for a, t in s["cool"].items() if t > tnow()}
    fresh = [a for a in discover() if a not in s["cool"]]
    pairs = pairs_for(fresh)
    for addr, p in pairs.items():
        reasons, bought = [], False
        for name, P in VARIANTS.items():
            V = s["v"][name]
            if not can_open(s, name) or addr in V["traded"]:
                continue
            ok, why = screen(p, P)
            if not ok:
                reasons.append(why)
                continue
            ok, why = rug_ok(addr, P["RC"], s)
            if not ok:
                reasons.append(why)
                continue
            buy(s, name, addr, p)
            bought = True
        if not bought:
            wait = 10 if "too young" in reasons else 30
            s["cool"][addr] = tnow() + wait * 60
    for a in [a for a, c in s["rc"].items() if tnow() - c["t"] > 6 * 3600]:
        s["rc"].pop(a)
    print(f"scan done: {len(fresh)} candidates, {sum(len(V['open']) for V in s['v'].values())} open")


# ---------- reporting ----------
def report():
    s = load()
    for name in VARIANTS:
        V = s["v"][name]
        pn = [c["pnl"] for c in V["closed"]]
        wins = [x for x in pn if x > 0]
        losses = [x for x in pn if x <= 0]
        eq = peak = dd = 0.0
        for x in pn:
            eq += x
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf") if wins else 0
        print(f"=== {name} ===")
        banked = sum(x["realized"] for x in V["open"].values())
        print(f"closed {len(pn)} | wins {len(wins)} ({len(wins) / max(len(pn), 1):.0%}) | open {len(V['open'])}"
              f" (profit already banked on open: ${banked:+.2f})")
        if pn:
            print(f"total P&L ${sum(pn):+.2f} | avg win ${sum(wins) / max(len(wins), 1):+.2f} | "
                  f"avg loss ${sum(losses) / max(len(losses), 1):+.2f}")
            print(f"profit factor {pf:.2f} | expectancy/trade ${sum(pn) / len(pn):+.2f} | max drawdown ${dd:.2f}")
        if len(pn) < 30:
            print(f"verdict: too early, need {30 - len(pn)} more closed trades")
        elif sum(pn) > 0 and pf >= 1.3:
            print("verdict: promising on paper (still not proof)")
        else:
            print("verdict: NOT working, do not trade real money with this")
        print(f"cash ${V['cash']:.2f}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "report":
        report()
    elif arg == "once":
        st = load()
        step(st)
        save(st)
    else:
        st = load()
        print("Paper bot running. Ctrl+C to stop.")
        while True:
            try:
                step(st)
                save(st)
            except KeyboardInterrupt:
                save(st)
                break
            except Exception as e:
                print("loop error:", e)
            time.sleep(300)
