"""
Packages the entire Telegram AI Butler project into a single deployable zip archive.
Excludes caches, test artifacts, and temp files, while preserving .env and authorized sessions.
"""

import os
import sys
import zipfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

def create_deploy_package():
    base_dir = Path(__file__).resolve().parent.parent
    output_zip = base_dir / "telegram_ai_butler_production.zip"

    exclude_patterns = [
        "__pycache__",
        ".pytest_cache",
        ".git",
        "*.pyc",
        "*.zip",
    ]

    print(f"📦 Arxiv yaratilmoqda: {output_zip.name}...")
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(base_dir):
            # Exclude unwanted directories
            dirs[:] = [d for d in dirs if d not in ["__pycache__", ".pytest_cache", ".git"]]
            
            for file in files:
                if file.endswith((".pyc", ".zip")) or file in ("sim_test.db", "temp_isolated.db", "telegram_ai_butler.db"):
                    continue
                if file.startswith("butler_backup_") or file.startswith("butler_dryrun_") or file.startswith("butler_pre_maint_"):
                    continue
                file_path = Path(root) / file
                rel_path = file_path.relative_to(base_dir)
                zipf.write(file_path, rel_path)
                print(f"  + {rel_path}")

    print(f"\n✅ Tayyor! Fayl hajmi: {output_zip.stat().st_size / 1024:.1f} KB")
    print(f"📁 Manzil: {output_zip}")
    return output_zip

if __name__ == "__main__":
    create_deploy_package()
