"""
Anti-ban and human-like interaction rate limiter.
Simulates typing delays and jitter to protect against Telegram MTProto spam detection.
"""

import asyncio
import random
from config import settings


class HumanRateLimiter:
    """
    Manages typing action simulation and delays.
    Mimics genuine human behavior to avoid Telegram SpamBot flags.
    """

    def __init__(self, min_delay: float = None, max_delay: float = None):
        self.min_delay = min_delay if min_delay is not None else getattr(settings, "HUMAN_DELAY_MIN", 1.0)
        self.max_delay = max_delay if max_delay is not None else getattr(settings, "HUMAN_DELAY_MAX", 2.5)

    def calculate_typing_duration(self, text: str) -> float:
        """
        Calculates realistic typing duration based on text length:
        Average typing speed: ~25-40 characters per second + short thinking jitter.
        """
        char_count = len(text)
        base_time = char_count / 35.0
        # Add random human pause (jitter)
        jitter = random.uniform(self.min_delay, self.max_delay)
        total = base_time + jitter
        # Clamp between min_delay and max_cap (respecting max_delay, up to 12.0s)
        max_cap = max(float(self.max_delay), 12.0)
        return max(float(self.min_delay), min(total, max_cap))

    async def simulate_human_reading(self, incoming_text_len: int):
        """Simulate time taken to read incoming message before replying."""
        # roughly 100-200 ms per word
        reading_time = min(max(incoming_text_len / 50.0, 1.0), 4.0)
        await asyncio.sleep(reading_time)
