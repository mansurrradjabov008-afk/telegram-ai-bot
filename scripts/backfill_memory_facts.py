import asyncio
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from config import settings
from database.crud import DatabaseManager
from services.memory_service import MemoryService

async def main():
    db = DatabaseManager()
    mem = MemoryService(db_manager=db)
    
    contacts = await db.list_contacts()
    print(f"Found {len(contacts)} contacts. Running fact extraction...")
    
    total_facts = 0
    for c in contacts:
        msgs = await db.get_mem_messages(c.user_id, limit=30)
        if not msgs:
            continue
        print(f"Extracting facts for {c.full_name} ({c.user_id}) from {len(msgs)} messages...")
        res = await mem.extract_facts_and_items(c.user_id, messages=msgs)
        facts = res.get("facts_stored", [])
        total_facts += len(facts)
        print(f"  -> Extracted {len(facts)} facts: {facts}")
        
    print(f"\nProcessing embeddings for newly extracted facts...")
    embedded = await mem.process_pending_embeddings(limit=50)
    print(f"Embedded {embedded} items.")
    print(f"Done! Total facts extracted: {total_facts}")

if __name__ == "__main__":
    asyncio.run(main())
