#!/usr/bin/env python3
"""Resolve Uniswap v4 pool parameters on Robinhood Chain.

A v4 pool ID is keccak256(poolKey) -- you cannot reverse it to learn the
pool's (fee, tickSpacing, hooks). This module reads the pool's actual
parameters from the PoolManager's Initialize event on-chain, so you never
have to guess.

Robinhood Chain specifics baked in:
  POOL_MANAGER  0x8366a39cc670b4001a1121b8f6a443a643e40951
  INIT_TOPIC    0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438
                (this fork's Initialize event signature -- NOT the canonical
                Uniswap v4 topic; verified against live chain logs)

Stdlib only. No web3, no API keys.

Usage as a library:
    from rh_v4_pools import resolve_pool
    pool, err = resolve_pool("0x0379E228F6887c6F18bf394042ECAF81B308cb2e")
    # pool -> {"pool_id": ..., "fee": 0, "tick_spacing": 200,
    #          "hooks": "0x...", "pair": {...dexscreener pair...}}

Usage as a CLI:
    python rh_v4_pools.py 0x0379E228F6887c6F18bf394042ECAF81B308cb2e
    python rh_v4_pools.py --pool-id 0x<64-hex>   # skip DexScreener

Environment:
    RH_RPC_URL   JSON-RPC endpoint (default: public Robinhood Chain RPC)
"""

import argparse
import json
import os
import sys
import urllib.request

# ---------------------------------------------------------------- constants

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INIT_TOPIC = ("0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WETH = "0x4200000000000000000000000000000000000006"  # WETH on Robinhood Chain

DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
CHUNK_BLOCKS = 10_000_000   # RPC getLogs window
MAX_CHUNKS = 9              # ~90M blocks of history
MIN_USD_LIQUIDITY = 5_000   # ignore dust pools when picking a route


def rpc_url():
    return os.environ.get("RH_RPC_URL", DEFAULT_RPC)


# ------------------------------------------------------------------ helpers

def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url(), data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "rh-v4-pool-discovery/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def _norm(addr):
    """Lowercase-normalize an address (case-insensitive on-chain)."""
    a = addr.lower()
    return a if a.startswith("0x") else "0x" + a


# ------------------------------------------------------------------ core

def discover_pool_params(pool_id, cache_path=None):
    """Return (fee, tick_spacing, hooks) for a v4 pool ID.

    Reads the Initialize event emitted by the PoolManager at deployment.
    Results are cached locally so repeat lookups never re-scan.
    Raises RuntimeError if no Initialize event is found.
    """
    pool_id = _norm(pool_id)
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}
    if pool_id in cache:
        c = cache[pool_id]
        return c["fee"], c["tick_spacing"], c["hooks"]

    latest = int(_rpc("eth_blockNumber", [])["result"], 16)
    for i in range(MAX_CHUNKS):
        frm = max(0, latest - (i + 1) * CHUNK_BLOCKS)
        to = latest - i * CHUNK_BLOCKS
        try:
            logs = _rpc("eth_getLogs", [{
                "address": POOL_MANAGER,
                "topics": [INIT_TOPIC, pool_id],
                "fromBlock": hex(frm), "toBlock": hex(to),
            }])
        except Exception:
            continue
        for log in logs.get("result", []) or []:
            data = (log.get("data") or "")[2:]
            if len(data) < 192:
                continue
            fee = int(data[0:64], 16)
            ts_raw = int(data[64:128], 16)
            tick_spacing = ts_raw if ts_raw < 2 ** 23 else ts_raw - 2 ** 24
            hooks = "0x" + data[128:192][-40:]
            if cache_path:
                cache[pool_id] = {"fee": fee, "tick_spacing": tick_spacing,
                                  "hooks": hooks}
                try:
                    json.dump(cache, open(cache_path, "w"))
                except Exception:
                    pass
            return fee, tick_spacing, hooks
    raise RuntimeError(
        f"no Initialize event for pool {pool_id} in last "
        f"{MAX_CHUNKS * CHUNK_BLOCKS // 1_000_000}M blocks")


def _dexscreener_pairs(token):
    req = urllib.request.Request(
        f"https://api.dexscreener.com/latest/dex/tokens/{token}",
        headers={"User-Agent": "rh-v4-pool-discovery/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("pairs", [])


def resolve_pool(token, cache_path=None):
    """Find working v4 pool params for a token on Robinhood Chain.

    Picks the most liquid v4 ETH/WETH pair from DexScreener, then discovers
    its on-chain parameters. Returns (pool_dict, None) on success or
    (None, reason) when no route exists.
    """
    token = _norm(token)
    try:
        pairs = _dexscreener_pairs(token)
    except Exception as e:
        return None, f"dexscreener fetch failed: {e}"

    v4_pairs = [p for p in pairs
                if p.get("chainId") == "robinhood"
                and "v4" in (p.get("labels") or [])]
    if not v4_pairs:
        return None, "no v4 pair on Robinhood Chain"

    def is_eth_route(p):
        q = ((p.get("quoteToken", {}) or {}).get("address") or "").lower()
        b = ((p.get("baseToken", {}) or {}).get("address") or "").lower()
        return (q in (ZERO_ADDRESS, WETH) or b in (ZERO_ADDRESS, WETH)) \
            and (p.get("liquidity", {}) or {}).get("usd", 0) > MIN_USD_LIQUIDITY

    eth_pairs = [p for p in v4_pairs if is_eth_route(p)]
    if not eth_pairs:
        best = max(v4_pairs,
                   key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0))
        q = (best.get("quoteToken", {}) or {}).get("symbol", "?")
        return None, f"no liquid v4 ETH/WETH route (best quoted in {q})"

    best = max(eth_pairs,
               key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0))
    pool_id = best.get("pairAddress")
    if not pool_id or not pool_id.startswith("0x"):
        return None, "dexscreener returned no pairAddress"

    try:
        fee, tick_spacing, hooks = discover_pool_params(pool_id, cache_path)
    except RuntimeError as e:
        return None, str(e)

    return {"pool_id": _norm(pool_id), "fee": fee,
            "tick_spacing": tick_spacing, "hooks": _norm(hooks),
            "dexscreener_url": best.get("url")}, None


# --------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Resolve Uniswap v4 pool params on Robinhood Chain")
    ap.add_argument("token", nargs="?",
                    help="token address (looked up via DexScreener)")
    ap.add_argument("--pool-id",
                    help="v4 pool ID (0x + 64 hex); skips DexScreener")
    ap.add_argument("--cache", default=".pool_params_cache.json",
                    help="local JSON cache path (default: ./.pool_params_cache.json)")
    ap.add_argument("--rpc", help="override RH_RPC_URL for this run")
    a = ap.parse_args(argv)

    if a.rpc:
        os.environ["RH_RPC_URL"] = a.rpc
    if not a.token and not a.pool_id:
        ap.error("provide a token address or --pool-id")

    if a.pool_id:
        try:
            fee, ts, hooks = discover_pool_params(a.pool_id, a.cache)
        except RuntimeError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        print(json.dumps({"ok": True, "pool_id": _norm(a.pool_id),
                          "fee": fee, "tick_spacing": ts,
                          "hooks": _norm(hooks)}, indent=2))
        return 0

    pool, err = resolve_pool(a.token, a.cache)
    if pool is None:
        print(json.dumps({"ok": False, "error": err}))
        return 1
    print(json.dumps({"ok": True, **pool}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
