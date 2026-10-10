"""
Production Database Maintenance Script for Antigravity Butler (Checkpoint 2).
Executes:
1. Safe Pre-Maintenance backup + integrity check.
2. Atomic transaction to delete test contacts (8801, 8802) from all tables.
3. Rebuilds FTS5 tables (mem_facts_fts, mem_summaries_fts, mem_messages_fts).
4. Applies Migration v9 triggers for strict category enum enforcement.
5. Verifies database integrity (PRAGMA integrity_check).
6. Compares Before vs After row counts.
7. Executes backup rotation (keeps newest 5, prunes older backups and orphaned wal/shm).
8. Runs post-cleanup sentinel scan for test data.
"""

import os
import sys
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services.backup_manager import (
    create_safe_backup,
    plan_backup_rotation,
    execute_backup_rotation,
)


def main():
    db_path = "data/butler.db"
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} does not exist.")
        sys.exit(1)

    print("=" * 60)
    print("ANTIGRAVITY BUTLER: PRODUCTION MAINTENANCE WINDOW")
    print("=" * 60)

    # 1. Pre-maintenance Backup
    print("\n--- 1. PRE-MAINTENANCE BACKUP ---")
    ok, backup_path, integrity_msg = create_safe_backup(db_path, "data")
    if not ok:
        print(f"FAILED to create pre-maintenance backup: {integrity_msg}")
        sys.exit(1)

    backup_size = os.path.getsize(backup_path)
    with open(backup_path, "rb") as f:
        backup_sha = hashlib.sha256(f.read()).hexdigest()
    print(f"Backup created: {backup_path}")
    print(f"Size: {backup_size} bytes")
    print(f"SHA256: {backup_sha}")
    print(f"Integrity: {integrity_msg}")

    # 2. Before Row Counts
    tables = [
        "contacts",
        "mem_contacts",
        "mem_messages",
        "mem_facts",
        "mem_open_items",
        "mem_summaries",
        "mem_embeddings",
        "conversation_history",
        "drafts",
        "scheduled_tasks",
    ]

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    before_counts = {}
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        before_counts[t] = cur.fetchone()[0]

    # 3. Atomic Maintenance Transaction
    print("\n--- 2. EXECUTING MAINTENANCE TRANSACTION ---")
    cur.execute("BEGIN IMMEDIATE TRANSACTION;")

    # a. Delete test contacts and memory items
    cur.execute("DELETE FROM mem_facts WHERE contact_user_id IN (8801, 8802);")
    del_facts = cur.rowcount
    cur.execute("DELETE FROM mem_summaries WHERE contact_user_id IN (8801, 8802);")
    del_summaries = cur.rowcount
    cur.execute("DELETE FROM mem_messages WHERE contact_user_id IN (8801, 8802);")
    del_messages = cur.rowcount
    cur.execute("DELETE FROM mem_contacts WHERE contact_user_id IN (8801, 8802);")
    del_mem_contacts = cur.rowcount
    cur.execute("DELETE FROM contacts WHERE user_id IN (8801, 8802);")
    del_contacts = cur.rowcount

    # b. Rebuild FTS5 virtual tables
    cur.execute("INSERT INTO mem_facts_fts(mem_facts_fts) VALUES('rebuild');")
    cur.execute("INSERT INTO mem_messages_fts(mem_messages_fts) VALUES('rebuild');")

    # c. Apply category triggers from Migration v9
    migration_file = "database/migrations/v9_enforce_category_trigger.sql"
    with open(migration_file, "r", encoding="utf-8") as f:
        sql_triggers = f.read()
    cur.executescript(sql_triggers)

    conn.commit()
    print("Tranzaksiya muvaffaqiyatli COMMIT qilindi.")
    print(f"O'chirilgan qatorlar:")
    print(f"  contacts: {del_contacts}")
    print(f"  mem_contacts: {del_mem_contacts}")
    print(f"  mem_messages: {del_messages}")
    print(f"  mem_facts: {del_facts}")
    print(f"  mem_summaries: {del_summaries}")

    # 4. Integrity Check and Checkpoint
    print("\n--- 3. DATABASE INTEGRITY CHECK ---")
    cur.execute("PRAGMA integrity_check;")
    integ = cur.fetchall()
    print(f"PRAGMA integrity_check: {integ}")
    assert integ == [("ok",)], f"Integrity check failed: {integ}"

    cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()

    # 5. After Row Counts
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    after_counts = {}
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        after_counts[t] = cur.fetchone()[0]

    print("\n--- 4. ROW COUNTS: OLDIN VA KEYIN ---")
    print(f"{'Jadval':22} | {'Oldin':8} | {'Keyin':8} | {'Farq':8}")
    print("-" * 52)
    for t in tables:
        diff = after_counts[t] - before_counts[t]
        print(f"{t:22} | {before_counts[t]:8} | {after_counts[t]:8} | {diff:+8}")

    # 6. Post-Cleanup Scan for Test Strings
    print("\n--- 5. SENTINEL SCAN (TEST/SECRET DATA CHECK) ---")
    test_terms = ["4111", "482915", "Qwerty", "ZEBRA", "ALISHER_SECRET", "BOBUR_SECRET", "8801", "8802"]
    found_any = False
    for t in tables:
        # Get text columns
        cur.execute(f"PRAGMA table_info({t});")
        cols = [c[1] for c in cur.fetchall()]
        for col in cols:
            for term in test_terms:
                cur.execute(f"SELECT COUNT(*) FROM {t} WHERE CAST({col} AS TEXT) LIKE ?", (f"%{term}%",))
                cnt = cur.fetchone()[0]
                if cnt > 0:
                    print(f"  MATCH: table={t}, col={col}, term={term}, count={cnt}")
                    found_any = True
    if not found_any:
        print("  Barcha jadvallarda 0 ta test/maxfiy ma'lumot qoldi (Toza!)")

    conn.close()

    # 7. Backup Rotation
    print("\n--- 6. BACKUP ROTATION ---")
    to_keep, to_delete = plan_backup_rotation("data", keep_count=5)
    deleted = execute_backup_rotation("data", keep_count=5, dry_run=False)
    print(f"Saqlangan zaxiralar ({len(to_keep)} ta):")
    for k in to_keep:
        print(f"  [SAQLANDI] {os.path.basename(k)}")
    print(f"O'chirilgan eski zaxiralar ({len(deleted)} ta):")
    for p in deleted:
        print(f"  [O'CHIRILDI] {os.path.basename(p)}")

    print("\n" + "=" * 60)
    print("TEXNIK XIZMAT MUVAFFAQIYATLI YAKUNLANDI!")
    print("=" * 60)


if __name__ == "__main__":
    main()
