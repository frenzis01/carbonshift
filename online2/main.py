"""
Online2 Batch Scheduler - Main Entry Point

Orchestrates:
1. Shared state management
2. Request generator (background thread)
3. Batch scheduler (background thread)
4. Monitoring and statistics
"""

import time
import signal
import sys
from typing import Optional
from shared_state import SharedSchedulerState
from request_generator import RequestGenerator
from scheduler import BatchScheduler
import config


class Online2System:
    """Main system orchestrator"""

    def __init__(self):
        """Initialize the system"""
        self.shared_state = SharedSchedulerState()
        self.request_generator = RequestGenerator(
            self.shared_state,
            requests_per_slot=config.REQUESTS_PER_SLOT
        )
        self.batch_scheduler = BatchScheduler(self.shared_state)

        # Flag for graceful shutdown
        self._running = True

        # Register signal handlers
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Handle graceful shutdown"""
        print("\n[Main] Shutdown signal received, stopping...")
        self._running = False

    def run(self, duration_seconds: Optional[float] = None):
        """
        Run the entire system.

        Args:
            duration_seconds: Run duration in seconds (None = run forever until signal)
        """
        print("="*80)
        print("Online2 Batch Scheduler")
        print("="*80)
        print(f"\nConfiguration:")
        print(f"  - Batch Size: {config.BATCH_SIZE}")
        print(f"  - Slot Duration: {config.SLOT_DURATION_SECONDS}s")
        print(f"  - Total Slots: {config.TOTAL_SLOTS}")
        print(f"  - Max Error: {config.MAX_ERROR_THRESHOLD}%")
        print(f"  - DP Pruning: {config.DP_PRUNING_STRATEGY}")
        print(f"  - Requests/Slot: {config.REQUESTS_PER_SLOT}")
        print()

        # Start components
        self.request_generator.start()
        self.batch_scheduler.start()

        # Monitor loop
        start_time = time.time()

        try:
            while self._running:
                elapsed = time.time() - start_time

                # Check if we've exceeded duration
                if duration_seconds and elapsed > duration_seconds:
                    print(f"\n[Main] Duration limit reached ({duration_seconds}s)")
                    break

                # Print statistics every 5 seconds
                if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                    self._print_statistics(elapsed)

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Main] Interrupted")

        finally:
            # Shutdown
            self.request_generator.stop()
            self.batch_scheduler.stop()

            print("\n" + "="*80)
            print("FINAL STATISTICS")
            print("="*80)
            self._print_statistics(time.time() - start_time, final=True)

            # Export results
            self.shared_state.export_to_csv(config.OUTPUT_FILE)
            print(f"\n✓ Results exported to {config.OUTPUT_FILE}")

    def _print_statistics(self, elapsed: float, final: bool = False):
        """Print current statistics"""
        stats = self.shared_state.get_statistics()
        gen_stats = {
            "generated": self.request_generator.get_total_generated()
        }
        sched_stats = self.batch_scheduler.get_statistics()

        print(f"\n[t={elapsed:.1f}s] Statistics:")
        print(f"  Generated: {gen_stats['generated']}")
        print(f"  Scheduled: {stats['total_scheduled']}")
        print(f"  Pending: {stats['pending']}")
        print(f"  Current Slot: {stats['current_slot']}")
        print(f"  Batches: {sched_stats['batches_processed']}")

        if final:
            print(f"\n  Throughput: {gen_stats['generated'] / elapsed:.2f} req/s")
            print(f"  Scheduling Rate: {stats['total_scheduled'] / elapsed:.2f} req/s")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="Online2 Batch Scheduler")
    parser.add_argument('--duration', type=float, default=None,
                        help='Run duration in seconds (default: run forever)')
    args = parser.parse_args()

    system = Online2System()
    system.run(duration_seconds=args.duration)
