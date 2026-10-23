"""
Tier-4 High-Availability Watchdog Supervisor for Antigravity AI Butler.
Continuous Health Probe & Deadlock Auto-Revive Daemon (Step 1.1).

Features:
- Subprocess lifecycle management (auto-restart on crash).
- Active Liveness Probe: polls http://127.0.0.1:7860/health every 15 seconds.
- Deadlock / Freeze Immunity: kills and respawns if 3 consecutive health probes fail.
- Graceful shutdown propagation on SIGINT/SIGTERM.
- Cross-platform: works on Windows, Linux, and Android/Termux.
"""

import os
import sys
import time
import signal
import subprocess
import urllib.request
import logging
from pathlib import Path

# Configure supervisor logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [WATCHDOG] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("WatchdogSupervisor")

BASE_DIR = Path(__file__).resolve().parent.parent
HEALTH_URL = "http://127.0.0.1:7860/health"
POLL_INTERVAL_SECONDS = 15.0
PROBE_TIMEOUT_SECONDS = 8.0
MAX_CONSECUTIVE_FAILURES = 5


def check_liveness_probe() -> bool:
    """Sends HTTP GET to the internal health endpoint."""
    try:
        req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "AntigravityWatchdog/1.0"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_process_rss_mb(pid: int) -> float:
    """Read Resident Set Size in MB from /proc/<pid>/status on Linux/Android."""
    try:
        status_file = Path(f"/proc/{pid}/status")
        if status_file.exists():
            for line in status_file.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return float(parts[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def run_supervisor():
    """Main supervisor loop."""
    logger.info("👑 Antigravity Tier-4 Watchdog Supervisor started.")
    logger.info(f"Target directory: {BASE_DIR}")
    logger.info(f"Monitoring endpoint: {HEALTH_URL} (probe every {POLL_INTERVAL_SECONDS}s)")

    python_executable = sys.executable
    main_py_path = str(BASE_DIR / "main.py")

    should_exit = False

    def handle_signal(sig, frame):
        nonlocal should_exit
        logger.info(f"Received exit signal ({sig}). Initiating supervisor shutdown...")
        should_exit = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not should_exit:
        logger.info("🚀 Spawning Antigravity Butler instance (python main.py)...")
        proc = subprocess.Popen(
            [python_executable, main_py_path],
            cwd=str(BASE_DIR),
        )
        logger.info(f"Butler instance started with PID: {proc.pid}")

        # Allow initial startup window before checking health (20 seconds for Android CPU)
        time.sleep(20.0)

        consecutive_failures = 0

        while not should_exit:
            # Check if process died on its own
            poll_res = proc.poll()
            if poll_res is not None:
                logger.warning(f"⚠️ Butler process (PID {proc.pid}) exited unexpectedly with code {poll_res}.")
                break

            # Check RSS memory limit (e.g. 350 MB max) to prevent Android Low Memory Killer
            rss_mb = get_process_rss_mb(proc.pid)
            if rss_mb > 350.0:
                logger.warning(
                    f"⚠️ High memory threshold reached: PID {proc.pid} using {rss_mb:.1f} MB (> 350 MB). "
                    "Initiating graceful memory recycling..."
                )
                try:
                    proc.terminate()
                    proc.wait(timeout=10.0)
                except Exception:
                    proc.kill()
                break

            # Run active liveness probe
            is_alive = check_liveness_probe()
            if is_alive:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                logger.warning(
                    f"⚠️ Health probe failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}) for PID {proc.pid}."
                )

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        f"🚨 DEADLOCK DETECTED! PID {proc.pid} unresponsive for {int(consecutive_failures * POLL_INTERVAL_SECONDS)}s."
                    )
                    logger.info(f"Force-terminating frozen process PID {proc.pid}...")
                    try:
                        proc.terminate()
                        proc.wait(timeout=5.0)
                    except Exception:
                        proc.kill()
                    logger.info("Frozen process terminated. Ready to respawn.")
                    break

            time.sleep(POLL_INTERVAL_SECONDS)

        # Clean up child process if supervisor is exiting
        if should_exit:
            logger.info(f"Shutting down child process PID {proc.pid}...")
            try:
                proc.terminate()
                proc.wait(timeout=5.0)
            except Exception:
                proc.kill()
            break

        logger.info("Cooling down for 3 seconds before next spawn cycle...")
        time.sleep(3.0)

    logger.info("🛑 Watchdog Supervisor stopped cleanly.")


if __name__ == "__main__":
    run_supervisor()
