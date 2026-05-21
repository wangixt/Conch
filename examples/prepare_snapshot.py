#!/usr/bin/env python3
"""
Helper script to create a snapshot for benchmark testing.

Usage:
    python3 examples/prepare_snapshot.py
    python3 examples/prepare_snapshot.py --image-name your-image
"""
import argparse
from conch import Sandbox


def main():
    parser = argparse.ArgumentParser(description="Create a snapshot for benchmark")
    parser.add_argument(
        '--image-name',
        type=str,
        default=None,
        help='Image name to use (uses config default if not provided)'
    )
    
    args = parser.parse_args()
    
    print("Creating sandbox...")
    sbx = Sandbox.create(image_name=args.image_name)
    
    print("Executing test command...")
    sbx.execute(cmd='python3', content='print("Snapshot ready")')
    
    print("Pausing sandbox to create snapshot...")
    snapshot_info = sbx.pause()
    
    print("\n" + "=" * 70)
    print("Snapshot created successfully!")
    print("=" * 70)
    print(f"Snapshot ID: {snapshot_info.snapshot_id}")
    print(f"Original Sandbox ID:  {snapshot_info.sandbox_id}")
    print("=" * 70)
    print("\nNote: Original sandbox is automatically cleaned up during pause.")
    print("\nYou can now use this snapshot ID for benchmark:")
    print(f"  python3 examples/benchmark_numa_concurrent.py --snapshot-id {snapshot_info.snapshot_id}")
    print("=" * 70)


if __name__ == "__main__":
    main()