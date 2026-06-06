"""Runtime configuration via environment variables (prefix USAGI_)."""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Path to the Usagi data folder that contains mainIndex/ and/or derivedIndex/.
    usagi_dir: str = ""

    # Path to Athena CONCEPT.csv (used to build the concept-name cache on first run).
    concept_csv: str = ""

    # SQLite cache path; defaults to <usagi_dir>/concepts.db
    concept_db_path: str = ""

    # Prefer derivedIndex when present (better IDF calibration for specific source sets).
    # Use False / mainIndex for a general-purpose API without a known source file.
    use_derived_index: bool = False

    default_top_n: int = 10

    model_config = {"env_prefix": "USAGI_"}

    def index_path(self) -> str:
        if self.use_derived_index:
            derived = os.path.join(self.usagi_dir, "derivedIndex")
            if os.path.exists(derived):
                return derived
        return os.path.join(self.usagi_dir, "mainIndex")

    def db_path(self) -> str:
        if self.concept_db_path:
            return self.concept_db_path
        return os.path.join(self.usagi_dir, "concepts.db")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
