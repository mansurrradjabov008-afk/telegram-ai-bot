"""
Authentication Helper for Telethon MTProto Userbot.
Handles requesting verification code and signing in.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from config import settings

TEMP_AUTH_FILE = root_dir / "data" / "auth_temp.json"


async def request_code():
    session_file = root_dir / "data" / settings.TELEGRAM_SESSION_NAME
    session_file.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_file), settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"ALREADY_AUTHORIZED:{me.id}:{me.first_name}")
        await client.disconnect()
        return

    phone = settings.TELEGRAM_PHONE_NUMBER
    try:
        sent = await client.send_code_request(phone)
        TEMP_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TEMP_AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"phone": phone, "phone_code_hash": sent.phone_code_hash}, f)
        print("CODE_REQUESTED_SUCCESSFULLY")
    except FloodWaitError as e:
        print(f"FLOOD_WAIT:{e.seconds}")
    except Exception as e:
        print(f"ERROR:{e}")
    finally:
        await client.disconnect()


async def verify_code(code: str, password: str = None):
    if not TEMP_AUTH_FILE.exists():
        print("ERROR_NO_PENDING_REQUEST")
        return

    with open(TEMP_AUTH_FILE, "r", encoding="utf-8") as f:
        auth_data = json.load(f)

    phone = auth_data["phone"]
    phone_code_hash = auth_data["phone_code_hash"]

    session_file = root_dir / "data" / settings.TELEGRAM_SESSION_NAME
    client = TelegramClient(str(session_file), settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
    await client.connect()

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        me = await client.get_me()
        print(f"AUTH_SUCCESS:{me.id}:{me.first_name}")
        if TEMP_AUTH_FILE.exists():
            os.remove(TEMP_AUTH_FILE)
    except SessionPasswordNeededError:
        if password:
            try:
                await client.sign_in(password=password)
                me = await client.get_me()
                print(f"AUTH_SUCCESS:{me.id}:{me.first_name}")
                if TEMP_AUTH_FILE.exists():
                    os.remove(TEMP_AUTH_FILE)
            except Exception as e:
                print(f"PASSWORD_ERROR:{e}")
        else:
            print("PASSWORD_NEEDED")
    except PhoneCodeInvalidError:
        print("INVALID_CODE")
    except Exception as e:
        print(f"VERIFY_ERROR:{e}")
    finally:
        await client.disconnect()


async def check_status():
    session_file = root_dir / "data" / settings.TELEGRAM_SESSION_NAME
    client = TelegramClient(str(session_file), settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
    await client.connect()
    is_auth = await client.is_user_authorized()
    if is_auth:
        me = await client.get_me()
        print(f"AUTHORIZED:{me.id}:{me.first_name}")
    else:
        print("NOT_AUTHORIZED")
    await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/auth_helper.py [request|verify <code> [password]|status]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "request":
        asyncio.run(request_code())
    elif cmd == "verify":
        code_arg = sys.argv[2] if len(sys.argv) > 2 else ""
        pass_arg = sys.argv[3] if len(sys.argv) > 3 else None
        asyncio.run(verify_code(code_arg, pass_arg))
    elif cmd == "status":
        asyncio.run(check_status())
