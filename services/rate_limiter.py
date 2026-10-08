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

    def __init__(self, min_delay: int = None, max_delay: int = None):
        self.min_delay = min_delay or settings.HUMAN_DELAY_MIN
        self.max_delay = max_delay or settings.HUMAN_DELAY_MAX

    def calculate_typing_duration(self, text: str) -> float:
        """
        Calculates realistic typing duration based on text length:
        Average typing speed: ~20-35 characters per second + thinking jitter.
        """
        char_count = len(text)
        base_time = char_count / 25.0
        # Add random human pause (jitter)
        jitter = random.uniform(self.min_delay, self.max_delay)
        total = base_time + jitter
        # Clamp between 2 seconds and 12 seconds max
        return max(float(self.min_delay), min(total, 12.0))

    async def simulate_human_reading(self, incoming_text_len: int):
        """Simulate time taken to read incoming message before replying."""
        # roughly 100-200 ms per word
        reading_time = min(max(incoming_text_len / 50.0, 1.0), 4.0)
        await asyncio.sleep(reading_time)
