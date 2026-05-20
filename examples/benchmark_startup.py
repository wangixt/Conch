#!/usr/bin/env python3
import time
import statistics
from conch import Sandbox

COLD_ITERATIONS = 5
SNAPSHOT_ITERATIONS = 10

def print_stats(name: str, times: list[float]):
    if not times:
        return
    print(f"\n{name} Statistics:")
    print(f"  Iterations: {len(times)}")
    print(f"  Min:        {min(times):.3f}s")
    print(f"  Max:        {max(times):.3f}s")
    print(f"  Avg:        {statistics.mean(times):.3f}s")
    print(f"  Median:     {statistics.median(times):.3f}s")
    if len(times) > 1:
        print(f"  Stddev:     {statistics.stdev(times):.3f}s")

def main():
    print("=" * 60)
    print("Conch Sandbox Startup Performance Benchmark")
    print("=" * 60)
    
    # Test 1: Cold start (create only)
    print(f"\n[Test 1: Cold Start - Create Only]")
    print(f"Running {COLD_ITERATIONS} iterations...")
    cold_create_times = []
    for i in range(COLD_ITERATIONS):
        t0 = time.perf_counter()
        sbx = Sandbox.create()
        t1 = time.perf_counter()
        cold_create_times.append(t1 - t0)
        print(f"  #{i+1}: {t1-t0:.3f}s")
        sbx.delete()
    print_stats("Cold Start (Create Only)", cold_create_times)
    
    # Test 2: Cold start + execute
    print(f"\n[Test 2: Cold Start - Create + Execute]")
    print(f"Running {COLD_ITERATIONS} iterations...")
    cold_exec_times = []
    for i in range(COLD_ITERATIONS):
        t0 = time.perf_counter()
        sbx = Sandbox.create()
        sbx.execute(cmd='python3', content='print(1)')
        t1 = time.perf_counter()
        cold_exec_times.append(t1 - t0)
        print(f"  #{i+1}: {t1-t0:.3f}s")
        sbx.delete()
    print_stats("Cold Start (Create + Execute)", cold_exec_times)
    
    # Test 3: Snapshot start (minimal snapshot - execute to activate vsock before pause)
    print("\n[Test 3: Snapshot Start - Minimal Snapshot]")
    sbx = Sandbox.create()
    #sbx.execute(cmd='echo', args=['ready'])
    snapshot_min = sbx.pause()
    print(f"Minimal snapshot created: {snapshot_min.snapshot_id}")
    
    print(f"Running {SNAPSHOT_ITERATIONS} iterations...")
    snapshot_min_times = []
    for i in range(SNAPSHOT_ITERATIONS):
        t0 = time.perf_counter()
        sbx2 = Sandbox.create(snapshot_min.snapshot_id)
        t1 = time.perf_counter()
        snapshot_min_times.append(t1 - t0)
        print(f"  #{i+1}: {t1-t0:.3f}s")
        sbx2.delete()
    print_stats("Snapshot Start (Minimal)", snapshot_min_times)
    
    # Test 4: Snapshot start (with execute before pause)
    print("\n[Test 4: Snapshot Start - After Execute]")
    sbx = Sandbox.create()
    sbx.execute(cmd='python3', content='print(1)')
    snapshot_exec = sbx.pause()
    print(f"Snapshot (after execute) created: {snapshot_exec.snapshot_id}")
    
    print(f"Running {SNAPSHOT_ITERATIONS} iterations...")
    snapshot_exec_times = []
    for i in range(SNAPSHOT_ITERATIONS):
        t0 = time.perf_counter()
        sbx2 = Sandbox.create(snapshot_exec.snapshot_id)
        t1 = time.perf_counter()
        snapshot_exec_times.append(t1 - t0)
        print(f"  #{i+1}: {t1-t0:.3f}s")
        sbx2.delete()
    print_stats("Snapshot Start (After Execute)", snapshot_exec_times)
    
    # Summary
    print(f"\n{'='*60}")
    print("Summary:")
    print(f"{'='*60}")
    
    cold_create_avg = statistics.mean(cold_create_times)
    cold_exec_avg = statistics.mean(cold_exec_times)
    snapshot_min_avg = statistics.mean(snapshot_min_times)
    snapshot_exec_avg = statistics.mean(snapshot_exec_times)
    
    print(f"Cold Start (create only):    {cold_create_avg:.3f}s")
    print(f"Cold Start (create+execute): {cold_exec_avg:.3f}s")
    print(f"Snapshot (minimal):          {snapshot_min_avg:.3f}s")
    print(f"Snapshot (after execute):    {snapshot_exec_avg:.3f}s")
    
    print(f"\nComparison:")
    print(f"  Snapshot(minimal) / Cold(create):     {snapshot_min_avg/cold_create_avg:.2f}x")
    print(f"  Snapshot(after exec) / Cold(create+exec): {snapshot_exec_avg/cold_exec_avg:.2f}x")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
