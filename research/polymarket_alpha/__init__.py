"""Polymarket smart-money / insider-trading detection pipeline.

Phase 1: identify wallets with verifiable above-market edge across many
resolved markets.
Phase 2: flag suspicious patterns - coordinated activity, big bets close
to non-obvious resolutions, win-rate concentration by market category.

Outputs are cached to data/polymarket_alpha/ so the pipeline is resumable.
"""
