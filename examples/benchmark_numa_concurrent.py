#!/usr/bin/env python3
import asyncio
import time
import statistics
import argparse
import sys
from typing import List, Dict, Tuple, Any
from concurrent.futures import ThreadPoolExecutor
from conch import Sandbox

DEFAULT_CONCURRENT_COUNT = 1000
DEFAULT_NUMA_NODES = 4
DEFAULT_BATCH_SIZE = 50
DEFAULT_TIMEOUT = 30
DEFAULT_RAM_MB = 1024


class ConcurrentBenchmark:
    def __init__(
        self,
        snapshot_id: str,
        numa_nodes: List[int],
        concurrent_count: int,
        batch_size: int,
        timeout: float,
        ram_mb: int,
    ):
        self.snapshot_id = snapshot_id
        self.numa_nodes = numa_nodes
        self.concurrent_count = concurrent_count
        self.batch_size = batch_size
        self.timeout = timeout
        self.ram_mb = ram_mb
        self.results: List[Tuple[str, int, float, bool, str, Any]] = []
        self.successful_sandboxes: List[Any] = []
        self.sandbox_ids: List[str] = []

    def allocate_numa_node(self, index: int) -> int:
        return self.numa_nodes[index % len(self.numa_nodes)]

    def create_sandbox_sync(self, index: int) -> Tuple[str, int, float, bool, str]:
        numa_node = self.allocate_numa_node(index)
        sandbox_id = f"benchmark-{index}-{numa_node}"

        start_time = time.perf_counter()
        success = False
        error_msg = ""
        created_sandbox = None

        try:
            sbx = Sandbox.create(
                snapshot_id=self.snapshot_id,
                numa_node=numa_node,
                sandbox_id=sandbox_id,
                vcpu_num=1,
                ram_mb=self.ram_mb,
            )
            sbx.execute(cmd='python3', content='print("Restored")')
            success = True
            created_sandbox = sbx
            self.sandbox_ids.append(sandbox_id)
        except Exception as e:
            error_msg = str(e)

        elapsed = time.perf_counter() - start_time
        return (sandbox_id, numa_node, elapsed, success, error_msg, created_sandbox)

    async def create_sandbox_batch(self, batch_indices: List[int]) -> List[Tuple[str, int, float, bool, str, Any]]:
        with ThreadPoolExecutor(max_workers=len(batch_indices)) as executor:
            loop = asyncio.get_event_loop()
            tasks = [
                loop.run_in_executor(executor, self.create_sandbox_sync, idx)
                for idx in batch_indices
            ]
            results = await asyncio.gather(*tasks)
            
            for result in results:
                if result[3] and result[5]:  # success and sandbox object exists
                    self.successful_sandboxes.append(result[5])
            
            return list(results)

    async def run_benchmark(self) -> Dict:
        print("=" * 70)
        print(f"Conch NUMA Concurrent Snapshot Startup Benchmark")
        print("=" * 70)
        print(f"Configuration:")
        print(f"  Snapshot ID:    {self.snapshot_id}")
        print(f"  NUMA Nodes:     {self.numa_nodes}")
        print(f"  Total Count:    {self.concurrent_count}")
        print(f"  Batch Size:     {self.batch_size}")
        print(f"  Timeout:        {self.timeout}s")
        print(f"  RAM Size:       {self.ram_mb}MB")
        print(f"  Allocation:     Round-robin across NUMA nodes")
        print(f"  Measure:        Create + Execute time (snapshot startup)")
        print("=" * 70)

        numa_distribution = {}
        for node in self.numa_nodes:
            numa_distribution[node] = self.concurrent_count // len(self.numa_nodes)
        if self.concurrent_count % len(self.numa_nodes) != 0:
            numa_distribution[self.numa_nodes[0]] += self.concurrent_count % len(self.numa_nodes)

        print(f"\nNUMA Distribution:")
        for node, count in numa_distribution.items():
            print(f"  NUMA {node}: {count} sandboxes")

        print(f"\nStarting benchmark...")
        overall_start = time.perf_counter()

        batch_count = (self.concurrent_count + self.batch_size - 1) // self.batch_size
        completed = 0

        for batch_idx in range(batch_count):
            start_idx = batch_idx * self.batch_size
            end_idx = min(start_idx + self.batch_size, self.concurrent_count)
            batch_indices = list(range(start_idx, end_idx))

            batch_start = time.perf_counter()
            batch_results = await asyncio.wait_for(
                self.create_sandbox_batch(batch_indices),
                timeout=self.timeout * len(batch_indices)
            )
            batch_elapsed = time.perf_counter() - batch_start

            self.results.extend(batch_results)
            completed += len(batch_results)

            batch_success = sum(1 for r in batch_results if r[3])
            batch_failed = len(batch_results) - batch_success

            print(f"  Batch {batch_idx+1}/{batch_count}: "
                  f"{len(batch_results)} sandboxes in {batch_elapsed:.2f}s "
                  f"(Success: {batch_success}, Failed: {batch_failed}) "
                  f"[{completed}/{self.concurrent_count}]")

        overall_elapsed = time.perf_counter() - overall_start

        return self.analyze_results(overall_elapsed, numa_distribution)

    def analyze_results(self, overall_elapsed: float, numa_distribution: Dict) -> Dict:
        success_count = sum(1 for r in self.results if r[3])
        failed_count = self.concurrent_count - success_count

        success_times = [r[2] for r in self.results if r[3]]
        failed_results = [(r[0], r[1], r[4]) for r in self.results if not r[3]]

        numa_stats = {}
        for node in self.numa_nodes:
            node_results = [r for r in self.results if r[1] == node]
            node_success = sum(1 for r in node_results if r[3])
            node_times = [r[2] for r in node_results if r[3]]
            numa_stats[node] = {
                'total': len(node_results),
                'success': node_success,
                'failed': len(node_results) - node_success,
                'times': node_times,
            }

        print("\n" + "=" * 70)
        print("Benchmark Results:")
        print("=" * 70)
        print(f"Overall:")
        print(f"  Total Time:           {overall_elapsed:.2f}s")
        print(f"  Total Sandboxes:      {self.concurrent_count}")
        print(f"  Successful:           {success_count} ({success_count/self.concurrent_count*100:.1f}%)")
        print(f"  Failed:               {failed_count} ({failed_count/self.concurrent_count*100:.1f}%)")

        if success_times:
            print(f"\n  Snapshot Startup Time Stats (Create + Execute):")
            print(f"    Min:                {min(success_times):.3f}s")
            print(f"    Max:                {max(success_times):.3f}s")
            print(f"    Avg:                {statistics.mean(success_times):.3f}s")
            print(f"    Median:             {statistics.median(success_times):.3f}s")
            if len(success_times) > 1:
                print(f"    Stddev:             {statistics.stdev(success_times):.3f}s")

            throughput = success_count / overall_elapsed
            print(f"\n  Throughput:           {throughput:.1f} sandboxes/s")

        print(f"\nNUMA Node Statistics:")
        for node in sorted(numa_stats.keys()):
            stats = numa_stats[node]
            print(f"\n  NUMA {node}:")
            print(f"    Total:              {stats['total']}")
            print(f"    Success:            {stats['success']} ({stats['success']/stats['total']*100:.1f}%)")
            print(f"    Failed:             {stats['failed']}")
            if stats['times']:
                print(f"    Avg Startup Time:   {statistics.mean(stats['times']):.3f}s (Create+Execute)")
                print(f"    Min Startup Time:   {min(stats['times']):.3f}s")
                print(f"    Max Startup Time:   {max(stats['times']):.3f}s")

        if failed_results and len(failed_results) <= 10:
            print(f"\nFailed Sandboxes (showing first 10):")
            for sandbox_id, numa_node, error in failed_results[:10]:
                print(f"  {sandbox_id} (NUMA {numa_node}): {error}")

        print("=" * 70)

        return {
            'overall_elapsed': overall_elapsed,
            'success_count': success_count,
            'failed_count': failed_count,
            'success_times': success_times,
            'numa_stats': numa_stats,
            'successful_sandboxes': self.successful_sandboxes,
        }

    def cleanup(self, max_concurrent: int = 50):
        if not self.successful_sandboxes:
            print("No successful sandboxes to cleanup")
            return
            
        print(f"\nCleaning up {len(self.successful_sandboxes)} successful sandboxes...")
        cleaned = 0
        failed = 0

        async def delete_batch(sandboxes: List[Any]):
            with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                loop = asyncio.get_event_loop()
                tasks = [
                    loop.run_in_executor(
                        executor,
                        lambda sbx: sbx.delete(),
                        sbx
                    )
                    for sbx in sandboxes
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                return results

        async def cleanup_async():
            nonlocal cleaned, failed
            batch_size = max_concurrent
            batches = [
                self.successful_sandboxes[i:i+batch_size]
                for i in range(0, len(self.successful_sandboxes), batch_size)
            ]

            for batch in batches:
                results = await delete_batch(batch)
                for idx, result in enumerate(results):
                    if isinstance(result, Exception):
                        failed += 1
                        print(f"  Failed to delete {batch[idx].sandbox_id}: {result}")
                    else:
                        cleaned += 1

        asyncio.run(cleanup_async())
        print(f"Cleanup completed: {cleaned} deleted, {failed} failed")


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark NUMA-aware concurrent snapshot startup"
    )
    parser.add_argument(
        '--snapshot-id',
        type=str,
        required=True,
        help='Snapshot ID to use (required)'
    )
    parser.add_argument(
        '--count',
        type=int,
        default=DEFAULT_CONCURRENT_COUNT,
        help=f'Number of concurrent sandboxes (default: {DEFAULT_CONCURRENT_COUNT})'
    )
    parser.add_argument(
        '--numa-nodes',
        type=str,
        default='0,1,2,3',
        help=f'NUMA nodes to use, comma-separated (default: 0,1,2,3)'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f'Batch size for concurrent creation (default: {DEFAULT_BATCH_SIZE})'
    )
    parser.add_argument(
        '--timeout',
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f'Timeout per sandbox creation in seconds (default: {DEFAULT_TIMEOUT})'
    )
    parser.add_argument(
        '--ram-mb',
        type=int,
        default=DEFAULT_RAM_MB,
        help=f'Memory size in MB - must match snapshot (default: {DEFAULT_RAM_MB})'
    )
    parser.add_argument(
        '--no-cleanup',
        action='store_true',
        help='Do not cleanup sandboxes after benchmark'
    )

    args = parser.parse_args()

    numa_nodes = [int(n.strip()) for n in args.numa_nodes.split(',')]

    benchmark = ConcurrentBenchmark(
        snapshot_id=args.snapshot_id,
        numa_nodes=numa_nodes,
        concurrent_count=args.count,
        batch_size=args.batch_size,
        timeout=args.timeout,
        ram_mb=args.ram_mb,
    )

    try:
        results = asyncio.run(benchmark.run_benchmark())
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user")
        if not args.no_cleanup:
            benchmark.cleanup()
        sys.exit(1)

    if not args.no_cleanup:
        benchmark.cleanup()

    print("\nBenchmark completed successfully!")


if __name__ == "__main__":
    main()