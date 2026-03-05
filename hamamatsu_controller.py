"""
Hamamatsu Detector Controller
=============================

Thread-safe controller for the Hamamatsu scintillation detector using USB.
Uses c12137_comm.C12137Device for all low-level USB communication.

Provides:
 - Continuous background acquisition in a thread
 - Cumulative 4096-channel spectrum
 - Real-time counts-per-second (CPS) estimate
 - Temperature and device time reporting
 - Timed (fixed-duration) acquisition
 - Periodic logging with delta_t and cumulative spectrum
 - Energy threshold get/set
 - Radiation limit get/set
 - EEPROM read
 - Internal temperature read
 - Module reset
"""

import os
import time
import threading
from typing import Optional, Tuple

import numpy as np

from c12137_comm import (
    C12137Device,
    RDMUSB_SUCCESS,
    RDMUSB_PACKET_ERROR,
    EEPROM_COMP_LEVEL,
    EEPROM_ENERGY_LOWER,
    EEPROM_ENERGY_UPPER,
    EEPROM_CONVERT_USV,
)

GE_TABLE_SIZE = 4096


class HamamatsuController:
    """
    High-level threaded controller for Hamamatsu C12137 detector.

    Uses C12137Device from c12137_comm for all USB communication.

    Provides:
      - connect(), disconnect()
      - start(), stop(), reset()
      - get_spectrum() -> (spectrum, elapsed, cps, temperature, device_time)
      - acquire_spectrum_for_duration()
      - start_periodic_logging() / stop_periodic_logging()
      - get/set energy threshold, radiation limits
      - read EEPROM, internal temperature, module reset
    """

    def __init__(self, verbose: int = 1):
        self.verbose = verbose

        self.dev = C12137Device()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()

        # Spectrum & timing
        self.spectrum = np.zeros(GE_TABLE_SIZE, dtype=np.uint32)
        self.elapsed_time = 0.0
        self._start_time = None

        # CPS estimation
        self.cps = 0.0
        self._cps_window = 3.0  # seconds
        self._history = []  # list of (time, total_counts)

        # Telemetry
        self.temperature = float("nan")
        self.device_time = 0.0
        self._last_pkt_index: Optional[int] = None
        self._time_overflows = 0

        # Logging
        self._log_thread: Optional[threading.Thread] = None
        self._log_stop_event = threading.Event()
        self._log_filename: Optional[str] = None
        self._log_interval: Optional[float] = None
        self._log_total_time: Optional[float] = None
        self.last_delta_t: Optional[float] = None

    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def connect(self) -> bool:
        """Find and open the detector USB device."""
        ok = self.dev.find_and_open()
        if ok and self.verbose:
            print("Device connected successfully.")
        elif not ok and self.verbose:
            print("ERROR: Device not found.")
        return ok

    def disconnect(self):
        """Stop acquisition and close USB."""
        if self._running:
            self.stop()
        self.dev.close()
        if self.verbose:
            print("Device disconnected.")

    @property
    def is_connected(self) -> bool:
        return self.dev.is_open

    # --------------------------------------------------------
    # START / STOP
    # --------------------------------------------------------

    def start(self):
        """Start background acquisition."""
        if self._running:
            return
        if not self.dev.is_open:
            if not self.connect():
                return
        if self.verbose:
            print("Starting HamamatsuController...")
        self.dev.clear_bulk_buffer()
        self._stop_event.clear()
        self._running = True
        self._start_time = time.time()
        self._last_pkt_index = None
        self._thread = threading.Thread(target=self._acquisition_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop acquisition and logging safely."""
        if not self._running:
            return
        if self.verbose:
            print("Stopping HamamatsuController...")
        self._stop_event.set()
        self.stop_periodic_logging()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._running = False
        if self.verbose:
            print("HamamatsuController stopped cleanly.")

    # --------------------------------------------------------
    # ACQUISITION LOOP
    # --------------------------------------------------------

    def _acquisition_loop(self):
        """Continuous acquisition loop using C12137Device bulk reads."""
        retry = 0
        max_retries = 10
        prev_pkt_index: Optional[int] = None

        while not self._stop_event.is_set():
            status, pkt_index, size, data, temp = self.dev.get_data_and_temperature()

            if status == RDMUSB_PACKET_ERROR:
                time.sleep(0.02)
                retry += 1
                if retry > max_retries:
                    self.dev.clear_bulk_buffer()
                    retry = 0
                continue
            elif status != RDMUSB_SUCCESS:
                time.sleep(0.02)
                self.dev.clear_bulk_buffer()
                retry += 1
                if retry > max_retries:
                    if self.verbose:
                        print("Too many read failures — stopping acquisition.")
                    break
                continue

            retry = 0

            # Skip duplicate packets
            if prev_pkt_index is not None and pkt_index == prev_pkt_index:
                time.sleep(0.02)
                continue

            # Track device time via packet index (increments every 100 ms)
            if prev_pkt_index is not None and prev_pkt_index > pkt_index + 60000:
                self._time_overflows += 1
            device_time = (65536 * self._time_overflows + pkt_index) / 10.0
            prev_pkt_index = pkt_index

            # Bin events into 4096-channel spectrum
            now = time.time()
            with self._lock:
                self.temperature = temp
                self.device_time = device_time
                self.elapsed_time = now - self._start_time

                for word in data:
                    address = (word >> 4) & 0x0FFF
                    if 0 <= address < GE_TABLE_SIZE:
                        self.spectrum[address] += 1

                total_counts = int(self.spectrum.sum())
                self._history.append((now, total_counts))
                self._history = [
                    (t, c) for t, c in self._history if now - t <= self._cps_window
                ]
                if len(self._history) > 1:
                    dt = self._history[-1][0] - self._history[0][0]
                    if dt > 0:
                        dc = self._history[-1][1] - self._history[0][1]
                        self.cps = dc / dt

            time.sleep(0.02)  # ~50 Hz polling

        if self.verbose:
            print("Acquisition loop exited cleanly.")

    # --------------------------------------------------------
    # DATA CONTROL
    # --------------------------------------------------------

    def reset(self):
        """Reset cumulative spectrum, timer, CPS, and history."""
        with self._lock:
            self.spectrum[:] = 0
            self._start_time = time.time()
            self.elapsed_time = 0.0
            self.cps = 0.0
            self._history.clear()
            self._time_overflows = 0
        if self.dev.is_open:
            self.dev.clear_bulk_buffer()
        if self.verbose:
            print("Spectrum reset.")

    def get_spectrum(self) -> Tuple[np.ndarray, float, float, float, float]:
        """
        Return a copy of the current spectrum, elapsed time, CPS,
        temperature, and device time.

        Automatically starts acquisition if not already running.
        """
        if not self._running:
            if self.verbose:
                print("Acquisition not running — starting automatically.")
            self.start()
            time.sleep(0.5)
        with self._lock:
            spec = np.copy(self.spectrum)
            elapsed = self.elapsed_time
            cps = self.cps
            temp = self.temperature
            dev_time = self.device_time
        return spec, elapsed, cps, temp, dev_time

    # --------------------------------------------------------
    # ENERGY THRESHOLD
    # --------------------------------------------------------

    def get_energy_threshold(self) -> Tuple[int, int]:
        """Return (status, threshold_index)."""
        return self.dev.get_energy_threshold()

    def set_energy_threshold(self, index_val: int) -> int:
        """Set the energy threshold (as channel index). Returns status."""
        return self.dev.set_energy_threshold(index_val)

    # --------------------------------------------------------
    # RADIATION LIMITS
    # --------------------------------------------------------

    def get_radiation_limit(self, area: int) -> Tuple[int, int]:
        """area=0 → lower, area=1 → upper.  Return (status, keV value)."""
        return self.dev.get_radiation_limit(area)

    def set_radiation_limit(self, area: int, value_kev: int) -> int:
        """Set radiation limit. area=0 → lower, area=1 → upper. Returns status."""
        return self.dev.set_radiation_limit(area, value_kev)

    # --------------------------------------------------------
    # EEPROM
    # --------------------------------------------------------

    def read_eeprom(self, address: int) -> Tuple[int, int]:
        """Read an EEPROM address. Return (status, data)."""
        return self.dev.read_eeprom(address)

    # --------------------------------------------------------
    # INTERNAL TEMPERATURE
    # --------------------------------------------------------

    def get_internal_temperature(self) -> Tuple[int, float]:
        """Return (status, celsius) from the I²C temperature sensor."""
        return self.dev.get_internal_temperature()

    # --------------------------------------------------------
    # MODULE RESET
    # --------------------------------------------------------

    def reset_module(self, level: int = 0) -> int:
        """Reset the detector module hardware. Returns status."""
        return self.dev.reset(level)

    # --------------------------------------------------------
    # TIMED ACQUISITION
    # --------------------------------------------------------

    def acquire_spectrum_for_duration(self, duration: float, filename: Optional[str] = None):
        """
        Acquire and return a spectrum for a fixed duration.

        Parameters
        ----------
        duration : float
            Acquisition duration in seconds.
        filename : str, optional
            If provided, save the spectrum to this file as plain text.

        Returns
        -------
        spectrum : np.ndarray
        elapsed : float
        """
        if not self._running:
            self.start()
        self.reset()
        start = time.time()
        while time.time() - start < duration and self._running:
            time.sleep(0.1)
        spec, elapsed, _, _, _ = self.get_spectrum()
        if filename:
            np.savetxt(filename, spec)
            if self.verbose:
                print(f"Spectrum ({elapsed:.1f}s) saved to {filename}")
        return spec, elapsed

    # --------------------------------------------------------
    # PERIODIC LOGGING
    # --------------------------------------------------------

    def start_periodic_logging(self, base_filename: str, interval: float = 10.0, total_time: float = 0.0):
        """
        Start logging cumulative spectrum every `interval` seconds.

        Parameters
        ----------
        base_filename : str
            Base filename (timestamp will be appended).
        interval : float
            Time between saves in seconds.
        total_time : float
            Total time to log for. If 0, log until stopped.
        """
        if self._log_thread and self._log_thread.is_alive():
            if self.verbose:
                print("Logging already active.")
            return

        # fresh spectrum for logging
        self.reset()

        dt_str = time.strftime("%Y%m%d_%H%M%S")
        root, ext = os.path.splitext(base_filename)
        if ext == "":
            ext = ".csv"
        filename = f"{root}_{dt_str}{ext}"

        self._log_filename = filename
        self._log_interval = interval
        self._log_total_time = total_time
        self._log_stop_event.clear()
        self.last_delta_t = 0.0

        with open(filename, "w") as f:
            f.write("delta_t," + ",".join([f"ch{i}" for i in range(4096)]) + "\n")

        self._log_thread = threading.Thread(target=self._logging_loop, daemon=True)
        self._log_thread.start()
        if self.verbose:
            dur = "indefinitely" if total_time == 0 else f"for {total_time}s"
            print(f"Periodic logging started ({interval}s interval, {dur}) -> {filename}")

    def _logging_loop(self):
        prev_time = time.time()
        start_time = prev_time
        while not self._log_stop_event.is_set():
            now = time.time()
            self.last_delta_t = now - prev_time
            prev_time = now

            spectrum, _, _, _, _ = self.get_spectrum()
            line = f"{self.last_delta_t:.3f}," + ",".join(map(str, spectrum)) + "\n"
            try:
                with open(self._log_filename, "a") as f:
                    f.write(line)
            except Exception as e:
                print(f"Error writing log file: {e}")

            if self._log_total_time > 0 and (now - start_time) >= self._log_total_time:
                break
            time.sleep(self._log_interval)
        if self.verbose:
            print("Logging loop ended.")

    def stop_periodic_logging(self):
        """Stop periodic logging if running."""
        if self._log_thread and self._log_thread.is_alive():
            self._log_stop_event.set()
            self._log_thread.join(timeout=2.0)
            if self.verbose:
                print("Periodic logging stopped.")
