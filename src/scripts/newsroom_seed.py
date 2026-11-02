"""
Loads the Newsroom starting lists (src/newsroom/seed/*.csv) into NewsAgent's database (MONGO_DB_NAME).

Default is a dry run: checks the lists, compares them with the database and
writes nothing. --apply inserts what is missing; existing entities, aliases
and ignore entries are never changed, so re-running is safe.

    python src/scripts/newsroom_seed.py
    python src/scripts/newsroom_seed.py --apply
"""
import argparse
import os
import sys
from pathlib import Path

from pymongo import MongoClient

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.configs.settings import settings
from src.newsroom.seed import SEED_DIR, apply_plan, build_plan, compare_with_db
from src.scheduler.db import _database_target, _mongo_client_options


def _print_counts(title: str, counts: dict) -> None:
    print(f"\n{title}")
    for key, value in counts.items():
        print(f"  {key:24} {value}")


def main(apply: bool, seed_dir: Path) -> int:
    plan = build_plan(seed_dir)
    _print_counts("Starting lists:", plan.counts())
    for warning in plan.warnings:
        print(f"  WARNING: {warning}")
    if plan.errors:
        print(f"\n{len(plan.errors)} error(s); nothing was written:")
        for error in plan.errors:
            print(f"  ERROR: {error}")
        return 1

    print(f"\n--- Database: {_database_target()} / {settings.MONGO_DB_NAME} | {'APPLY' if apply else 'DRY RUN'} ---")
    client = MongoClient(settings.DATABASE_URL, **_mongo_client_options())
    try:
        client.admin.command("ping")
        db = client[settings.MONGO_DB_NAME]
        _print_counts("Compared with the database:", compare_with_db(plan, db))
        if not apply:
            print("\nDry run only. Re-run with --apply to insert the missing records.")
            return 0
        _print_counts("Applied:", apply_plan(plan, db))
        return 0
    finally:
        client.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Load the Newsroom starting lists (dry run by default).")
    parser.add_argument("--apply", action="store_true", help="Insert missing records (default: dry run, no writes).")
    parser.add_argument("--seed-dir", type=Path, default=SEED_DIR, help="Folder with the three CSV files.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(main(args.apply, args.seed_dir))
