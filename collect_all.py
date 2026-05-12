# backwards-compat stub — use ingestion.collect directly
# Run: python -m ingestion.collect [--kaggle] [--twscrape] [--accounts accounts.txt]
from ingestion.collect import main

if __name__ == "__main__":
    raise SystemExit(main())
