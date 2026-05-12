# backwards-compat stub — use ingestion.merger directly
from ingestion.merger import load_merged, build_merged, merge_sources

__all__ = ["load_merged", "build_merged", "merge_sources"]
