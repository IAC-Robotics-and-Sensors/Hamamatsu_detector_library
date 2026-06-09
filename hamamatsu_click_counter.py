"""
Hamamatsu click counter for Raspberry Pi.
=========================================

This script starts the Hamamatsu detector, records cumulative counts to a CSV
file, and pulses a GPIO output once for each newly detected count.

Designed for a Raspberry Pi driving an active buzzer, clicker, or small
speaker module from a single GPIO pin.

Examples:
    python hamamatsu_click_counter.py --pin 7 --interval 0.1 --duration 300
    python hamamatsu_click_counter.py --pin 7 --sample 0.05 --output counts.csv

Dependencies:
    pip install numpy pyusb
    On Raspberry Pi for clicking output:
    pip install gpiozero
    or use the built-in RPi.GPIO package if available.
"""

from __future__ import annotations

import argparse
import csv
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

from hamamatsu_controller import HamamatsuController


class Clicker:
    """GPIO pulse generator with a no-op fallback when GPIO is unavailable."""

    def __init__(self, pin: int, pulse_s: float = 0.01, gap_s: float = 0.01):
        self.pin = pin
        self.pulse_s = pulse_s
        self.gap_s = gap_s
        self._gpio = None
        self._active = False

        try:
            import RPi.GPIO as gpio  # type: ignore

            self._gpio = gpio
            gpio.setwarnings(False)
            gpio.setmode(gpio.BOARD)
            gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
            self._active = True
        except Exception:
            self._gpio = None
            self._active = False

    @property
    def available(self) -> bool:
        return self._active

    def click_once(self):
        if not self._active:
            return
        self._gpio.output(self.pin, self._gpio.HIGH)
        time.sleep(self.pulse_s)
        self._gpio.output(self.pin, self._gpio.LOW)
        if self.gap_s > 0:
            time.sleep(self.gap_s)

    def close(self):
        if self._active and self._gpio is not None:
            self._gpio.output(self.pin, self._gpio.LOW)
            self._gpio.cleanup(self.pin)
        self._active = False


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record Hamamatsu counts and click once per detected count."
    )
    parser.add_argument(
        "--pin",
        type=int,
        default=7,
        help="Physical BOARD pin used to drive the clicker/buzzer (default: 7).",
    )
    parser.add_argument(
        "--interval",
        "--sample",
        dest="sample",
        type=float,
        default=0.1,
        help="Polling interval in seconds for count accumulation (default: 0.1).",
    )
    parser.add_argument(
        "--pulse",
        type=float,
        default=0.01,
        help="GPIO high time for each click in seconds (default: 0.01).",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=0.01,
        help="Pause between clicks in seconds (default: 0.01).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Optional run duration in seconds. Use 0 to run until Ctrl+C.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("hamamatsu_click_counts.csv"),
        help="CSV file for recorded count history.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detector status and per-interval updates.",
    )
    return parser


def click_worker(clicker: Clicker, click_queue: queue.Queue[int], stop_event: threading.Event):
    """Drain queued counts and emit one pulse per count."""
    while not stop_event.is_set() or not click_queue.empty():
        try:
            count = click_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        for _ in range(max(0, count)):
            if stop_event.is_set() and click_queue.empty():
                break
            clicker.click_once()

        click_queue.task_done()


def main() -> int:
    args = build_arg_parser().parse_args()

    controller = HamamatsuController(verbose=args.verbose)
    clicker = Clicker(args.pin, pulse_s=args.pulse, gap_s=args.gap)
    click_queue: queue.Queue[int] = queue.Queue()
    stop_event = threading.Event()
    worker = threading.Thread(
        target=click_worker,
        args=(clicker, click_queue, stop_event),
        daemon=True,
    )

    if args.verbose:
        if clicker.available:
            print(f"GPIO clicker ready on physical pin {args.pin} (BOARD numbering).")
        else:
            print("GPIO backend not available; counts will still be recorded.")

    if not controller.connect():
        print("Could not connect to the Hamamatsu detector.")
        clicker.close()
        return 1

    controller.start()
    controller.reset()
    worker.start()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    start_time = time.time()
    last_total = 0

    with args.output.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["wall_time_s", "elapsed_s", "total_counts", "delta_counts", "cps", "temperature_c", "device_time_s"])

        try:
            while True:
                if args.duration > 0 and time.time() - start_time >= args.duration:
                    break

                spectrum, elapsed, cps, temp, device_time = controller.get_spectrum()
                total_counts = int(np.sum(spectrum))
                delta_counts = max(0, total_counts - last_total)
                last_total = total_counts

                if delta_counts:
                    click_queue.put(delta_counts)

                writer.writerow(
                    [
                        round(time.time() - start_time, 3),
                        round(elapsed, 3),
                        total_counts,
                        delta_counts,
                        round(cps, 3),
                        round(temp, 3) if np.isfinite(temp) else "nan",
                        round(device_time, 3),
                    ]
                )
                csv_file.flush()

                if args.verbose:
                    print(
                        f"elapsed={elapsed:7.2f}s total={total_counts:8d} +{delta_counts:4d} cps={cps:8.1f} temp={temp:6.1f}C"
                    )

                time.sleep(args.sample)

        except KeyboardInterrupt:
            print("Interrupted by user.")
        finally:
            stop_event.set()
            click_queue.join()
            worker.join(timeout=3.0)
            controller.stop()
            controller.disconnect()
            clicker.close()

    print(f"Saved count history to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())