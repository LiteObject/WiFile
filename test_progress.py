#!/usr/bin/env python3
"""Simple test to demonstrate the progress bar functionality."""

from wifile import show_progress, format_bytes
import time
import sys
import os

# Add the current directory to path to import wifile
sys.path.append(os.path.dirname(__file__))


def test_progress_bar():
    """Test the progress bar with simulated file transfer."""
    print("Testing progress bar functionality...")

    # Simulate a 5MB file transfer
    total_size = 5 * 1024 * 1024  # 5MB
    chunk_size = 64 * 1024  # 64KB chunks

    print(f"Simulating transfer of {format_bytes(total_size)} file...")

    transferred = 0
    start_time = time.time()

    while transferred < total_size:
        # Simulate network delay
        time.sleep(0.01)

        # Transfer a chunk
        chunk = min(chunk_size, total_size - transferred)
        transferred += chunk

        # Update progress
        show_progress(transferred, total_size, start_time)

    print("Transfer simulation complete!")


if __name__ == "__main__":
    test_progress_bar()
