# Hamamatsu Detector Control & GUI

A Python interface for collecting gamma-ray spectra from a **Hamamatsu** scintillation detector over USB.

This project provides:
- A **thread-safe controller class (`HamamatsuController`)** for data acquisition.
- A **Tkinter + Matplotlib GUI (`hamamatsu_gui.py`)** for live monitoring, timed acquisition, and periodic logging.
- An **example script** for programmatic use (`hamamatsu_example_acquisition.py`).

---

## Features

- Automatic USB device discovery (`idVendor=0x0661`, `idProduct=0x2917`)
- Optional USB hub power cycling via `uhubctl` to recover freezes
- Continuous background acquisition in a thread
- 4096-channel cumulative spectrum (12-bit -> 16-channel binning)
- Real-time counts-per-second (CPS) estimate from a sliding time window
- Temperature and device time reporting
- Timed (fixed-duration) acquisitions
- Periodic logging to timestamped CSV files with per-spectrum `Δt`
- Clean shutdown and thread-safe access to data

---

## Project Structure

```
.
├── hamamatsu_controller.py        # Threaded detector controller
├── hamamatsu_gui.py               # Interactive GUI with live spectrum
├── hamamatsu_example_acquisition.py # Example script for timed/periodic runs
└── README_hamamatsu.md
```

---

## Installation

You’ll need Python ≥ 3.8.

Install Python dependencies:

```bash
pip install -r requirements_hamamatsu.txt
```

System dependencies:
- `uhubctl` (optional, but recommended on Raspberry Pi / USB hub systems for power cycling)
```
sudo apt install uhubctl
```

---

## Usage

### GUI

Run the live GUI:

```bash
python hamamatsu_gui.py
```

Features:
- **Start / Stop** acquisition
- **Reset** spectrum and timing
- **Save Spectrum** to text
- **Acquire Fixed Spectrum** for N seconds
- **Periodic Logging** with interval + total time
- Live status bar with **elapsed time**, **CPS**, **temperature**, and **Δt** during logging

---

### Programmatic Example

Run:

```bash
python hamamatsu_example_acquisition.py
```

This will:
1. Perform a 10-second timed acquisition, save it to `hamamatsu_timed_spectrum.txt`, and show a static plot.
2. Perform a 1-minute periodic acquisition, logging spectra every 15 seconds to a timestamped CSV and showing a live plot.

---

## Output Files

### Timed Spectrum
`hamamatsu_timed_spectrum.txt`  
→ Text file with 4096 counts (one per channel).

### Periodic Log
`<base>_YYYYMMDD_HHMMSS.csv`  
Example: `hamamatsu_periodic_log_20251104_143512.csv`

Each row contains:

```text
delta_t,ch0,ch1,...,ch4095
10.002,0,1,2,1,...
9.998,5,3,2,4,...
```

Where:
- `delta_t` is the time between this saved spectrum and the previous one (seconds).
- `chN` are cumulative counts in each spectrum channel.

---

## API Quick Reference

### Class: `HamamatsuController`

| Method | Description |
|--------|-------------|
| `start()` | Connect and begin background acquisition |
| `stop()` | Stop acquisition and release USB resources |
| `reset()` | Reset cumulative spectrum, cps and timer |
| `get_spectrum()` | Return `(spectrum, elapsed_time, cps, temperature, device_time)` |
| `acquire_spectrum_for_duration(duration, filename=None)` | Timed acquisition (cumulative) |
| `start_periodic_logging(base_filename, interval, total_time=0)` | Periodic CSV logging |
| `stop_periodic_logging()` | Stop ongoing logging |
| `cps` | Current counts per second (sliding window) |
| `last_delta_t` | Last Δt between logged spectra |

---

## Docker (Optional)

You can adapt your D3S Docker setup by:
- Installing `pyusb`, `numpy`, `matplotlib`, `tk`
- Ensuring the container has access to the USB device (`--device` flag)
- Installing `uhubctl` in the container if you want power cycling

Example:

```bash
docker run -it --device=/dev/bus/usb:/dev/bus/usb hamamatsu-controller
```

Then inside:

```bash
python hamamatsu_gui.py
```

---

## Notes

- This interface is designed to be robust in long-running deployments (e.g. Raspberry Pi + USB hub).
- If the detector drops out, the controller attempts to reinitialize it transparently.
- For debugging, set `verbose=True` when creating the `HamamatsuController`.
