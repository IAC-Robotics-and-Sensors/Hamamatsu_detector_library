"""
Example: HamamatsuController Timed and Periodic Acquisitions
===========================================================

Demonstrates:
 1. A fixed-duration timed acquisition with a simple plot.
 2. A periodic logging acquisition with a live updating plot.

Requirements:
    pip install pyusb numpy matplotlib
"""

import time
import numpy as np
import matplotlib.pyplot as plt

from hamamatsu_controller import HamamatsuController


def example_timed_acquisition():
    """
    Example 1: Acquire a single spectrum for a fixed duration and plot it.
    """
    print("\n=== Hamamatsu Timed Acquisition Example ===")
    ctrl = HamamatsuController(verbose=True)
    ctrl.start()

    duration = 10.0  # seconds
    filename = "hamamatsu_timed_spectrum.txt"

    print(f"Acquiring spectrum for {duration:.1f} seconds...")
    spectrum, elapsed = ctrl.acquire_spectrum_for_duration(duration, filename)

    # Plot result
    plt.figure(figsize=(8, 5))
    plt.plot(spectrum, color="blue")
    plt.title(f"Hamamatsu Timed Acquisition ({elapsed:.1f}s)")
    plt.xlabel("Channel")
    plt.ylabel("Counts")
    plt.grid(True)
    plt.tight_layout()
    plt.show(block=False)

    print(f"\nAcquisition complete.")
    print(f"Elapsed time: {elapsed:.2f} s")
    print(f"Spectrum saved to: {filename}")
    print(f"Total counts: {int(np.sum(spectrum))}\n")

    ctrl.stop()
    time.sleep(1)


def example_periodic_logging():
    """
    Example 2: Periodic logging with live plot.

    Logs cumulative spectra to a CSV file every `interval` seconds while
    showing a live updating spectrum plot.
    """
    print("\n=== Hamamatsu Periodic Logging Example ===")
    ctrl = HamamatsuController(verbose=True)
    ctrl.start()

    interval = 15.0  # seconds between saved spectra
    total_time = 60.0  # run for 1 minute for demo
    base_filename = "hamamatsu_periodic_log"

    print(f"Starting periodic logging every {interval}s for {total_time}s...")
    ctrl.start_periodic_logging(base_filename, interval, total_time)

    # Live updating plot
    plt.ion()
    fig, ax = plt.subplots(figsize=(8, 5))
    line, = ax.plot(np.zeros(4096), color="green")
    ax.set_title("Hamamatsu Periodic Logging (Cumulative Spectrum)")
    ax.set_xlabel("Channel")
    ax.set_ylabel("Counts")
    ax.grid(True)
    plt.tight_layout()

    try:
        while ctrl._log_thread and ctrl._log_thread.is_alive():
            spectrum, elapsed, cps, temp, _ = ctrl.get_spectrum()
            line.set_ydata(spectrum)
            ax.relim()
            ax.autoscale_view(True, True, True)
            fig.canvas.draw()
            fig.canvas.flush_events()

            dt = ctrl.last_delta_t or 0.0
            print(
                f"Elapsed: {elapsed:.1f}s | CPS: {cps:.1f} | Temp: {temp:.1f}°C | Δt: {dt:.2f}s"
            )
            time.sleep(5)
    except KeyboardInterrupt:
        print("Interrupted — stopping logging.")
        ctrl.stop_periodic_logging()

    ctrl.stop()
    plt.ioff()
    print(f"\nLogging complete. File written to {ctrl._log_filename}\n")


if __name__ == "__main__":
    print("Hamamatsu Detector Example Program")
    print("==================================")
    print("1. Timed Acquisition (10 s)")
    print("2. Periodic Logging (15 s interval for 1 min)\n")

    try:
        example_timed_acquisition()
        example_periodic_logging()
    except KeyboardInterrupt:
        print("\nUser interrupted. Stopping acquisition.")
