"""
Hamamatsu Detector Controller
=============================

Thread-safe controller for the Hamamatsu scintillation detector using USB.

This provides a high-level interface similar to the D3SController:
 - Continuous background acquisition in a thread
 - Cumulative 4096-channel spectrum
 - Real-time counts-per-second (CPS) estimate
 - Temperature and device time reporting
 - Timed (fixed-duration) acquisition
 - Periodic logging with delta_t and cumulative spectrum
"""

import os
import time
import threading
from typing import Optional, Tuple

import numpy as np
import usb.core
import usb.util
from struct import unpack


class HamamatsuDetector:
    """Low-level interface to the Hamamatsu detector over USB."""

    def __init__(self, port=None, uhubctl=False, verbose: int = 1, virtual: bool = False):
        self.port = port
        self.uhubctl = uhubctl
        self.timeOverflows = 0
        self.previousTimeIndex = 0
        self.status = "pre init"
        self.verbose = verbose
        self.virtualDevice = virtual
        self.timeIndex = 0
        self.temperature = float("nan")
        self.deviceTime = 0.0

        tic = time.time()

        if self.virtualDevice:
            self.bootDuration = 0
            self.status = "OK"
            return

        # power cycle, if requested
        if self.uhubctl is None:
            time.sleep(1)
        else:
            self.powerCycle()

        # find device
        if not self.getDevice():
            return

        # set up USB endpoint
        self.device.reset()
        self.device.reset()  # belt & braces
        self.device.set_configuration()
        self.status = "initializing"

        self.configuration = self.device.get_active_configuration()
        self.interface = self.configuration[(0, 0)]
        self.endpoint = self.interface[0]
        self.ep = self.endpoint
        self.maxPacketSize8 = self.ep.wMaxPacketSize
        self.maxPacketSize16 = self.ep.wMaxPacketSize // 2
        self.bootDuration = time.time() - tic
        self.status = "OK"

    def powerCycle(self):
        """Power-cycle USB hub to fix freezing (requires `uhubctl` on the system)."""
        if os.system("which uhubctl >/dev/null 2>&1") != 0:
            if self.verbose:
                print("uhubctl not installed — skipping power cycle.")
            return

        if isinstance(self.uhubctl, str):
            addressString = self.uhubctl
        elif self.uhubctl:  # True: try to infer from port
            if self.port is None:
                self.getDevice()
            addressString = " -l 1-"
            for val in self.port[:-1]:
                addressString += f"{val}."
            addressString = addressString[:-1] + f" -p {self.port[-1]}"
        else:
            return

        if self.verbose > 0:
            os.system(f"uhubctl -a off {addressString}")
            time.sleep(3)
            os.system(f"uhubctl -a on {addressString}")
            time.sleep(3)
        elif self.verbose == 0:
            print(f"Powercycling {addressString}")
            os.system(f"uhubctl -a off {addressString} >/dev/null 2>&1")
            time.sleep(3)
            os.system(f"uhubctl -a on {addressString} >/dev/null 2>&1")
            time.sleep(3)
        self.status = "power cycle complete"

    def getDevice(self) -> bool:
        """Find the Hamamatsu USB device."""
        devices = list(usb.core.find(idVendor=0x0661, idProduct=0x2917, find_all=True))
        if len(devices) == 0:
            device = None
            self.status = "device not found"
        else:
            if self.port is None:
                if len(devices) == 1:
                    device = devices[0]
                else:
                    print("Multiple Hamamatsus detected; please specify port.")
                    self.status = "multiple devices"
                    device = None
            else:
                foundDevice = False
                for device in devices:
                    if device.port_numbers == self.port:
                        foundDevice = True
                        break
                if not foundDevice:
                    self.status = "device not found"
                    device = None

        if device is None:
            self.device = None
            print("ERROR: Could not find Hamamatsu at port", self.port)
            return False

        self.device = device
        self.port = self.device.port_numbers
        self.status = "device found"
        return True

    def processHeader(self) -> bool:
        """Read and parse the dataframe header."""
        if self.virtualDevice:
            self.detectorEvents = 1000
            self.timeIndex = (self.timeIndex + 1) % 65336
            self.tempADC = 50000
        else:
            while True:
                try:
                    self.data = self.device.read(
                        self.ep.bEndpointAddress, self.ep.wMaxPacketSize, 100
                    )
                    (
                        self.headerStart,
                        self.detectorEvents,
                        self.timeIndex,
                        self.tempADC,
                    ) = unpack(">LHxxHHxxxx", self.data[0:16])
                except Exception:
                    return False
                if self.headerStart == 1515870810:
                    break
                if self.verbose > 0:
                    print(
                        f"Bad header start value {self.headerStart} - resyncing data frame"
                    )
            self.headerData = self.data[16:]

        # time overflow handling
        if self.previousTimeIndex - self.timeIndex > 65000:
            self.timeOverflows += 1
        self.deviceTime = (65536 * self.timeOverflows + self.timeIndex) / 10.0
        # temperature
        self.temperature = 188.686 - 0.00348 * self.tempADC
        return True

    def processReadings(self) -> bool:
        """Read the 12-bit channel data into self.channels."""
        self.channels = np.zeros(1048, dtype=np.uint16)
        if self.virtualDevice:
            self.channels[:1000] = np.random.randint(0, 65336, 1000, dtype=np.uint16)
            return True

        try:
            # first 24 channels from header remnant
            self.channels[:24] = unpack("<" + "H" * 24, self.headerData)
            self.headerData = None
            # remaining channels
            for address in range(24, 1048, self.maxPacketSize16):
                self.data = self.device.read(
                    self.ep.bEndpointAddress, self.ep.wMaxPacketSize, 100
                )
                self.channels[address : address + self.maxPacketSize16] = unpack(
                    "<" + "H" * self.maxPacketSize16, self.data
                )
        except Exception:
            return False
        return True

    def binChannels(self, binning: int = 16):
        """Bin raw channels into 4096-channel spectrum bins."""
        self.channelBinning = binning
        self.binnedChannels = np.floor_divide(self.channels, self.channelBinning)


class HamamatsuController:
    """
    High-level threaded controller for Hamamatsu detector.

    Provides:
      - start(), stop(), reset()
      - get_spectrum() -> (spectrum, elapsed, cps, temperature, device_time)
      - acquire_spectrum_for_duration()
      - start_periodic_logging() / stop_periodic_logging()
    """

    def __init__(
        self,
        port=None,
        uhubctl=True,
        verbose: int = 1,
        virtual: bool = False,
    ):
        self.port = port
        self.uhubctl = uhubctl
        self.verbose = verbose
        self.virtual = virtual

        self.detector: Optional[HamamatsuDetector] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()

        # Spectrum & timing
        self.spectrum = np.zeros(4096, dtype=np.uint32)
        self.elapsed_time = 0.0
        self._start_time = None

        # CPS estimation
        self.cps = 0.0
        self._cps_window = 3.0  # seconds
        self._history = []  # list of (time, total_counts)

        # Telemetry
        self.temperature = float("nan")
        self.device_time = 0.0

        # Logging
        self._log_thread: Optional[threading.Thread] = None
        self._log_stop_event = threading.Event()
        self._log_filename: Optional[str] = None
        self._log_interval: Optional[float] = None
        self._log_total_time: Optional[float] = None
        self.last_delta_t: Optional[float] = None

    # --------------------------------------------------------
    # START / STOP
    # --------------------------------------------------------

    def start(self):
        """Start background acquisition."""
        if self._running:
            return
        if self.verbose:
            print("Starting HamamatsuController...")
        self._stop_event.clear()
        self._running = True
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._acquisition_loop)
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
        if self.detector and hasattr(self.detector, "device"):
            try:
                usb.util.dispose_resources(self.detector.device)
            except Exception:
                pass
        if self.verbose:
            print("HamamatsuController stopped cleanly.")

    # --------------------------------------------------------
    # ACQUISITION LOOP
    # --------------------------------------------------------

    def _acquisition_loop(self):
        """Continuous acquisition loop with automatic device handling."""
        while not self._stop_event.is_set():
            # (Re)create detector
            self.detector = HamamatsuDetector(
                port=self.port,
                uhubctl=self.uhubctl,
                verbose=self.verbose,
                virtual=self.virtual,
            )
            if self.detector.status != "OK":
                if self.verbose:
                    print("Detector init failed, retrying in 2 seconds...")
                time.sleep(2)
                continue

            looptime = 0.1
            loop_step = 0.001

            if self.verbose:
                print("Hamamatsu setup complete. Entering acquisition loop...")

            while not self._stop_event.is_set():
                try:
                    time.sleep(max(0.0, looptime))

                    if not self.detector.processHeader():
                        if self.verbose:
                            print("Header read failed — restarting.")
                        break
                    if not self.detector.processReadings():
                        if self.verbose:
                            print("Channel read failed — restarting.")
                        break

                    # Bin channels (protect against missing data)
                    if hasattr(self.detector, "channels") and len(self.detector.channels) > 0:
                        self.detector.binChannels(16)
                        if not hasattr(self.detector, "binnedChannels"):
                            if self.verbose:
                                print("binChannels() failed — skipping this frame.")
                            continue
                    else:
                        if self.verbose:
                            print("No channel data — skipping this frame.")
                        continue

                    counts = np.bincount(
                        self.detector.binnedChannels[: self.detector.detectorEvents],
                        minlength=4096,
                    ).astype(np.uint32)

                    now = time.time()
                    with self._lock:
                        self.spectrum += counts
                        self.elapsed_time = now - self._start_time
                        self.temperature = self.detector.temperature
                        self.device_time = self.detector.deviceTime

                        total_counts = int(self.spectrum.sum())
                        self._history.append((now, total_counts))
                        self._history = [(t, c) for t, c in self._history if now - t <= self._cps_window]
                        if len(self._history) > 1:
                            dt = self._history[-1][0] - self._history[0][0]
                            if dt > 0:
                                dc = self._history[-1][1] - self._history[0][1]
                                self.cps = dc / dt

                except Exception as e:
                    if self.verbose:
                        print(f"Acquisition error: {e}")
                    break


            if self.verbose:
                print("Lost communication with detector, attempting restart...")

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

        self._log_thread = threading.Thread(target=self._logging_loop)
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
