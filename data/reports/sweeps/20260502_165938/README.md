# Parameter Sweep Report

Generated outputs:
- `leaderboard_dev_all_competitors.csv`: Stage A/B development leaderboard for all competitors.
- `leaderboard_stage_c_holdout.csv`: held-out finalist stress test leaderboard.
- `top10_configs.json`: full params and metrics for the top 10 held-out finalists.
- `top3_attribution.json`: alpha/cost/risk-rule attribution for the top 3.
- `worst10_failure_analysis.json`: concise diagnosis for fragile profiles.
- `winner_sensitivity.csv`: local perturbation test around the selected winner.
- `finalist_configs/`: ready-to-run `run.py paper` config payloads.

Important limitation: this historical runner cannot reconstruct true historical CLOB prices unless you add an appended order-book or picks snapshot log. It therefore uses a fixed baseline model as the synthetic market maker, then applies current execution/risk/cost rules consistently across all competitors.
