"""
Isolation Guard for Database Operations.
Enforces that tests, scratch scripts, and shadow checks NEVER run against
the production database (data/butler.db).
"""

from pathlib import Path
from config import settings


def ensure_not_production_db(db_path: Path | str, caller_name: str = "Script") -> None:
    """
    Enforces that test/scratch runners NEVER point to production data/butler.db.
    Raises RuntimeError if resolved db_path equals the production database path.
    """
    resolved = Path(db_path).resolve()
    prod_path = (settings.BASE_DIR / "data" / "butler.db").resolve()
    if resolved == prod_path:
        raise RuntimeError(
            f"SAFETY GUARD ACTIVATED: {caller_name} is strictly forbidden from executing against production database '{prod_path}'!"
        )
