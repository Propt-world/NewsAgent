import sys
import os

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.utils import tracing
from src.utils.backfill import run_country_backfill

if __name__ == "__main__":
    tracing.init_tracing()  # Phoenix, when PHOENIX_COLLECTOR_ENDPOINT is set (one trace per article)
    run_country_backfill()
