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

This repo is intended for a linux OS, but will function on WSL with a bit of setup

<details>

<summary> Linux OS (e.g. Ubuntu) </summary>

### Linux OS (e.g. Ubuntu)

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

</details>

<details>

<summary> Windows Subsystem for Linux (WSL) </summary>

### Windows Subsystem for Linux (WSL)

If you are not using Linux or don't want to set up dual boot you can use WSL to access the functionality of linux alongside your windows OS.

If you have not already installed WSL, open an administrator powershell terminal and type the following:

```
wsl --install
```

More details on WSL installation can be found at https://learn.microsoft.com/en-us/windows/wsl/install

Following the instructions here: https://learn.microsoft.com/en-us/windows/wsl/connect-usb#attach-a-usb-device, first download and run the latest usbipd-win.msi file 
(should look like eg 'usbipd-win_5.3.0_x64.msi')

With the detector plugged into a usb port, back in powershell (admin) run the following to show the available usb devices:
```
usbipd list
```
this should show something like the following:
```
PS C:\Users\em22501> usbipd list
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-3    0bda:557a  Integrated Webcam, Camera DFU Device                          Not shared
1-12   0661:2917  Unknown device                                                Attached
1-14   8087:0033  Intel(R) Wireless Bluetooth(R)                                Not shared
```

In this example case the hamamatsu is the 'Unknown device' at ```<busid>=1-12```. Your device may differ. To check which one it is, simply run usbipd list again with the detector unplugged and see which device is missing. Note that different usb ports on your computer may give different values here and may require separate bindings.

Now bind the device to the usbipd configuration
```
usbipd bind --busid <busid>
```

And with wsl running (start it in another terminal if you haven't already) attach the bound device to the open WSL instance:
- NOTE YOU NEED TO DO THIS ATTACHING STEP EVERY TIME YOU START UP WSL OR UN/REPLUG IN THE DEVICE
If you want you can set up a script to do this more quickly without having to do it all in the command line which will save time


```
usbipd attach --wsl --busid <busid>
```

Now in the wsl terminal check the usb devices:
```
lsusb
```

this should return something like the following:
```
em22501@IT107326:/mnt/c/Users/em22501$ lsusb
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 001 Device 002: ID 0661:2917 Hamamatsu Photonics K.K.
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
```

Make a note of the ID of the detector, in this example case: 0661:2917. This will be used later and corresponds to ```<idVendor>:<idProduct>```. This should remain static for the same detector, though different individual devices may differ.

Now you will almost certainly encounter a 'USBError: [Errno 13] Access denied (insufficient permissions)' error if you attempt to run the example acquisition script at this point. In this case you need to define a permission rule for your ubuntu system to associate with the detector. To do this, following instructions here: https://discuss.pylabrobot.org/t/dealing-with-usberror-errno-13-access-denied-insufficient-permissions-on-debian/52, in your linux file system go to
```
/etc/udev/rules.d/
```
and create a new rules file by opening a new document with super user power:
```
sudo nano /etc/udev/rules.d/99-usb.rules
```
In the text editor add the following line (using the idVendor and idProduct from earlier) and save:
```
SUBSYSTEM=="usb", ATTR{idVendor}=="<idVendor>", ATTR{idProduct}=="<idProduct>", MODE="0666"
```

Now reset the udev rules system and trigger the new rule:
```
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Finally restart WSL, re-attach the detector and try to run the example acquisition script to test the setup.

</details>

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
