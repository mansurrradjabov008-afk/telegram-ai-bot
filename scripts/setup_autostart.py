"""
Script to register Antigravity Butler in Windows user Startup folder.
Ensures the bot starts automatically whenever the PC is turned on/logged in.
"""

import os
import sys
from pathlib import Path


def setup_startup_shortcut():
    startup_dir = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    shortcut_path = startup_dir / "AntigravityButler.lnk"

    script_dir = Path(__file__).resolve().parent.parent
    vbs_path = script_dir / "run_silently.vbs"

    # Create shortcut using win32com or PowerShell script file
    ps_content = f"""
$wsh = New-Object -ComObject WScript.Shell
$sc = $wsh.CreateShortcut('{str(shortcut_path)}')
$sc.TargetPath = 'wscript.exe'
$sc.Arguments = '"{str(vbs_path)}"'
$sc.WorkingDirectory = '{str(script_dir)}'
$sc.Description = 'Antigravity Telegram AI Executive Butler Auto-Start'
$sc.Save()
"""
    ps_file = script_dir / "scripts" / "_temp_create_shortcut.ps1"
    with open(ps_file, "w", encoding="utf-8") as f:
        f.write(ps_content)

    res = os.system(f'powershell -ExecutionPolicy Bypass -File "{ps_file}"')
    if ps_file.exists():
        ps_file.unlink()

    if shortcut_path.exists():
        print(f"SUCCESS: Shortcut created at {shortcut_path}")
        return True
    else:
        print("FAILED to create shortcut.")
        return False


if __name__ == "__main__":
    setup_startup_shortcut()
