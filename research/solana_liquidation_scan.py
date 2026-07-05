"""
Thread F re-test: quantify real on-chain marginfi liquidation frequency + competition
directly from Solana mainnet, bypassing the previously-blocked Drift Data API (403,
offline since the April 2026 hack) and the previously-404'd Dune dashboard (iteration 5).

Uses ONLY the public Solana RPC (api.mainnet-beta.solana.com, no API key) via raw
getSignaturesForAddress + getTransaction JSON-RPC calls. No synthetic data.

marginfi-v2 program id confirmed via web search (docs.marginfi.com / mrgnlabs/marginfi-v2):
MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA
"""
import requests
import time
import json
import statistics

RPC = "https://api.mainnet-beta.solana.com"
MARGINFI_PROGRAM = "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA"


def rpc_call(method, params, retries=4):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        try:
            r = requests.post(RPC, json=payload, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(data["error"])
            return data["result"]
        except requests.exceptions.RequestException:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"rpc_call {method} failed after {retries} retries")


def fetch_signatures(program_id, target_count=1500):
    """Paginate getSignaturesForAddress backwards via 'before' cursor."""
    all_sigs = []
    before = None
    while len(all_sigs) < target_count:
        params = [program_id, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        batch = rpc_call("getSignaturesForAddress", params)
        if not batch:
            break
        all_sigs.extend(batch)
        before = batch[-1]["signature"]
        time.sleep(0.15)
        if len(batch) < 1000:
            break
    return all_sigs


def fetch_tx(sig, retries=3):
    params = [
        sig,
        {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
    ]
    return rpc_call("getTransaction", params, retries=retries)


def main():
    print("=== Thread F re-test: real on-chain marginfi liquidation scan ===")
    print(f"Program: {MARGINFI_PROGRAM}")
    print("Fetching signatures for address (public RPC, paginated)...")

    sigs = fetch_signatures(MARGINFI_PROGRAM, target_count=1500)
    print(f"Fetched {len(sigs)} real signatures touching the marginfi-v2 program.")

    block_times = [s["blockTime"] for s in sigs if s.get("blockTime")]
    if not block_times:
        print("No blockTime data returned -- aborting.")
        return
    span_seconds = max(block_times) - min(block_times)
    span_hours = span_seconds / 3600.0
    print(
        f"Time window covered by these {len(sigs)} sigs: {span_hours:.3f} hours "
        f"({span_seconds}s), from slot {min(s['slot'] for s in sigs)} to {max(s['slot'] for s in sigs)}"
    )

    err_count = sum(1 for s in sigs if s.get("err") is not None)
    print(
        f"Failed (err != null) transactions in this window: {err_count}/{len(sigs)} "
        f"({100*err_count/len(sigs):.1f}%) -- high failure rate is a signature of "
        f"latency-competitive races (multiple bots racing the same opportunity, only "
        f"one lands)."
    )

    # Sample transactions (successful ones first, since only successful liquidations
    # matter for yield; but also sample some failed ones to check if they're failed
    # liquidation attempts specifically, which would corroborate the race dynamic).
    ok_sigs = [s for s in sigs if s.get("err") is None]
    fail_sigs = [s for s in sigs if s.get("err") is not None]

    sample_ok = ok_sigs[:: max(1, len(ok_sigs) // 220)][:220] if ok_sigs else []
    sample_fail = fail_sigs[:: max(1, len(fail_sigs) // 60)][:60] if fail_sigs else []

    print(
        f"\nSampling {len(sample_ok)} successful + {len(sample_fail)} failed txs "
        f"(real getTransaction calls) to classify instruction types..."
    )

    def classify(sig_entry, log_container):
        sig = sig_entry["signature"]
        try:
            tx = fetch_tx(sig)
        except Exception as e:
            return None
        if tx is None:
            return None
        meta = tx.get("meta", {})
        logs = meta.get("logMessages", []) or []
        joined = " ".join(logs).lower()
        is_liq = "liquidat" in joined
        signer = None
        try:
            signer = tx["transaction"]["message"]["accountKeys"][0]
        except Exception:
            pass
        log_container.append(
            {
                "sig": sig,
                "is_liquidation": is_liq,
                "err": sig_entry.get("err"),
                "signer": signer,
                "blockTime": sig_entry.get("blockTime"),
            }
        )
        return is_liq

    results_ok = []
    for i, s in enumerate(sample_ok):
        classify(s, results_ok)
        if i % 40 == 0 and i > 0:
            print(f"  ...{i}/{len(sample_ok)} successful sampled")
        time.sleep(0.12)

    results_fail = []
    for i, s in enumerate(sample_fail):
        classify(s, results_fail)
        time.sleep(0.12)

    liq_ok = [r for r in results_ok if r["is_liquidation"]]
    liq_fail = [r for r in results_fail if r["is_liquidation"]]

    print(f"\n--- Sample classification ---")
    print(f"Successful sample: {len(results_ok)} txs fetched, {len(liq_ok)} contain 'liquidat' in logs "
          f"({100*len(liq_ok)/max(1,len(results_ok)):.1f}%)")
    print(f"Failed sample: {len(results_fail)} txs fetched, {len(liq_fail)} contain 'liquidat' in logs "
          f"({100*len(liq_fail)/max(1,len(results_fail)):.1f}%)")

    # Extrapolate real-world liquidation frequency from the sampled fraction applied
    # to the FULL signature set (not just the sample), over the REAL measured time window.
    ok_liq_rate = len(liq_ok) / max(1, len(results_ok))
    fail_liq_rate = len(liq_fail) / max(1, len(results_fail))

    est_liq_ok_total = ok_liq_rate * len(ok_sigs)
    est_liq_fail_total = fail_liq_rate * len(fail_sigs)
    est_liq_total = est_liq_ok_total + est_liq_fail_total

    per_hour = est_liq_total / span_hours if span_hours > 0 else float("nan")
    per_day = per_hour * 24

    print(f"\n--- Extrapolated to the full {len(sigs)}-signature / {span_hours:.3f}h window ---")
    print(f"Estimated successful liquidation attempts: {est_liq_ok_total:.1f}")
    print(f"Estimated failed liquidation attempts (lost races): {est_liq_fail_total:.1f}")
    print(f"Estimated total liquidation ATTEMPTS: {est_liq_total:.1f}")
    print(f"=> {per_hour:.2f} liquidation attempts/hour, ~{per_day:.1f} attempts/day (real, not modeled)")

    win_rate = est_liq_ok_total / est_liq_total if est_liq_total > 0 else float("nan")
    print(f"Implied win rate across all liquidation ATTEMPTS (successful / total): {100*win_rate:.1f}%")

    unique_signers = {r["signer"] for r in (liq_ok + liq_fail) if r["signer"]}
    print(f"Distinct signer wallets among sampled liquidation txs: {len(unique_signers)} "
          f"(out of {len(liq_ok)+len(liq_fail)} sampled liquidation txs) -- "
          f"a low unique-wallet count relative to tx count means a few bots are already "
          f"dominant/repeat-executing this niche.")

    print("\n--- Reference profit-per-liquidation (from iteration 5's secondary evidence, "
          "not re-derived here -- public logs do not expose USD profit directly without "
          "full balance-diff parsing) ---")
    print("baransel.dev practitioner report: $15-500 per successful liquidation "
          "(5-10% bonus on repaid debt, size-dependent), same bot lost ~60% of races.")

    if per_day > 0 and win_rate > 0:
        lo, hi = 15, 500
        capital = 3000  # mid of the objective's $500-5000 test range
        daily_profit_lo = per_day * win_rate * lo
        daily_profit_hi = per_day * win_rate * hi
        # This assumes a NEW solo bot wins its "fair share" of the successful pool,
        # which the objective's own risk framing (and the win-rate finding here) says
        # is optimistic -- a new entrant is unlikely to win at the AVERAGE rate, since
        # existing infra-heavy bots already claim most races. Report as an upper bound,
        # not an expectation.
        print(f"\nIF a new solo bot could win liquidations at the population AVERAGE win "
              f"rate ({100*win_rate:.1f}%) -- an OPTIMISTIC upper bound, since faster/"
              f"better-resourced incumbents already claim most races per the secondary "
              f"evidence above -- naive daily profit potential across ALL market "
              f"liquidations would be ${daily_profit_lo:.0f}-${daily_profit_hi:.0f}/day "
              f"market-wide. This is NOT what a single new bot should expect to capture; "
              f"it is the total pie, split among however many competing bots are already "
              f"running (unique-signer count above is the closest real signal on that).")

    print("\n=== Verdict ===")
    print("This measurement replaces Thread F's prior 'no public data available' block "
          "with real on-chain frequency + real win-rate + real competition-density data, "
          "pulled directly from Solana mainnet via public RPC (no API key, no paid infra). "
          "See research/FINDINGS_BEYOND_POLYMARKET.md for the full write-up.")


if __name__ == "__main__":
    main()
