"""
Hamamatsu Detector GUI
======================

Tkinter + Matplotlib GUI for the HamamatsuController.

Features:
 - Start / Stop acquisition
 - Reset spectrum
 - Save current spectrum
 - Fixed-duration (timed) acquisition
 - Periodic logging (interval + total time)
 - Live plot with CPS, temperature, and delta_t display
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import time
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from hamamatsu_controller import HamamatsuController


class HamamatsuGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Hamamatsu Detector Control")
        self.root.geometry("950x750")

        self.controller = HamamatsuController(verbose=True)
        self.running = False
        self._shutdown = False
        self.update_thread = None

        self._setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --------------------------------------------------------
    # UI SETUP
    # --------------------------------------------------------

    def _setup_ui(self):
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(frame, text="Start", command=self.start).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame, text="Stop", command=self.stop).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame, text="Reset", command=self.controller.reset).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame, text="Save Spectrum", command=self.save_spectrum).pack(side=tk.LEFT, padx=5)

        # Timed acquisition
        ttk.Label(frame, text="Acquire Duration (s):").pack(side=tk.LEFT, padx=5)
        self.acquire_time_var = tk.StringVar(value="10")
        ttk.Entry(frame, width=6, textvariable=self.acquire_time_var).pack(side=tk.LEFT)
        ttk.Button(frame, text="Acquire Fixed Spectrum", command=self.acquire_fixed_spectrum).pack(side=tk.LEFT, padx=5)

        # Logging controls
        log_frame = ttk.LabelFrame(self.root, text="Periodic Logging", padding=10)
        log_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)

        ttk.Label(log_frame, text="Interval (s):").pack(side=tk.LEFT, padx=5)
        self.interval_var = tk.StringVar(value="10")
        ttk.Entry(log_frame, width=6, textvariable=self.interval_var).pack(side=tk.LEFT)

        ttk.Label(log_frame, text="Total Time (s, 0 = continuous):").pack(side=tk.LEFT, padx=5)
        self.total_var = tk.StringVar(value="0")
        ttk.Entry(log_frame, width=6, textvariable=self.total_var).pack(side=tk.LEFT)

        ttk.Button(log_frame, text="Start Logging", command=self.start_logging).pack(side=tk.LEFT, padx=5)
        ttk.Button(log_frame, text="Stop Logging", command=self.stop_logging).pack(side=tk.LEFT, padx=5)

        # Matplotlib plot
        fig, ax = plt.subplots(figsize=(8, 5))
        self.fig, self.ax = fig, ax
        self.ax.set_title("Hamamatsu Live Spectrum")
        self.ax.set_xlabel("Channel")
        self.ax.set_ylabel("Counts")
        self.line, = self.ax.plot(np.arange(4096), np.zeros(4096), color="blue")
        self.canvas = FigureCanvasTkAgg(fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status, relief=tk.SUNKEN, anchor="w").pack(side=tk.BOTTOM, fill=tk.X)

    # --------------------------------------------------------
    # CORE CONTROLS
    # --------------------------------------------------------

    def start(self):
        self.controller.start()
        if not self.running:
            self.running = True
            self.update_thread = threading.Thread(target=self._update_loop)
            self.update_thread.start()
        self.status.set("Acquisition started.")

    def stop(self):
        self.running = False
        self.controller.stop()
        self.status.set("Acquisition stopped.")

    def save_spectrum(self):
        filename = filedialog.asksaveasfilename(defaultextension=".txt")
        if filename:
            spec, elapsed, _, _, _ = self.controller.get_spectrum()
            np.savetxt(filename, spec)
            messagebox.showinfo("Saved", f"Spectrum ({elapsed:.1f}s) saved to:\n{filename}")

    def acquire_fixed_spectrum(self):
        try:
            duration = float(self.acquire_time_var.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid duration.")
            return
        filename = filedialog.asksaveasfilename(defaultextension=".txt", title="Save acquired spectrum")
        if not filename:
            return

        # Reset display & spectrum
        self.controller.reset()
        self.update_plot(np.zeros(4096))
        self.status.set(f"Acquiring for {duration}s...")

        threading.Thread(
            target=self._acquire_fixed_thread,
            args=(duration, filename),
            daemon=True,
        ).start()

    def _acquire_fixed_thread(self, duration, filename):
        spec, elapsed = self.controller.acquire_spectrum_for_duration(duration, filename)
        messagebox.showinfo("Saved", f"Spectrum acquired ({elapsed:.1f}s) and saved to:\n{filename}")
        self.status.set(f"Timed spectrum saved ({elapsed:.1f}s)")

    # --------------------------------------------------------
    # LOGGING
    # --------------------------------------------------------

    def start_logging(self):
        try:
            interval = float(self.interval_var.get())
            total = float(self.total_var.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid interval or total time.")
            return
        filename = filedialog.asksaveasfilename(defaultextension=".csv", title="Base filename for logging")
        if not filename:
            return

        # Reset display & spectrum before logging
        self.controller.reset()
        self.update_plot(np.zeros(4096))
        self.controller.start_periodic_logging(filename, interval, total)
        self.status.set(f"Logging started ({interval}s interval)")

    def stop_logging(self):
        self.controller.stop_periodic_logging()
        self.status.set("Logging stopped")

    # --------------------------------------------------------
    # UPDATE LOOP
    # --------------------------------------------------------

    def _update_loop(self):
        while self.running and not self._shutdown:
            spectrum, elapsed, cps, temp, dev_time = self.controller.get_spectrum()
            self.update_plot(spectrum)
            dt = self.controller.last_delta_t
            if dt:
                self.status.set(
                    f"Elapsed: {elapsed:.1f}s | CPS: {cps:.1f} | Temp: {temp:.1f}°C | Δt: {dt:.2f}s"
                )
            else:
                self.status.set(
                    f"Elapsed: {elapsed:.1f}s | CPS: {cps:.1f} | Temp: {temp:.1f}°C"
                )
            time.sleep(0.25)
        print("Update thread exited cleanly.")

    def update_plot(self, spectrum):
        self.line.set_ydata(spectrum)
        self.ax.relim()
        self.ax.autoscale_view(True, True, True)
        self.canvas.draw_idle()

    # --------------------------------------------------------
    # SHUTDOWN
    # --------------------------------------------------------

    def on_close(self):
        """Clean shutdown of GUI and controller."""
        if messagebox.askokcancel("Quit", "Are you sure you want to exit?"):
            self._shutdown = True
            self.status.set("Closing...")
            try:
                self.stop()
            except Exception as e:
                print(f"Error stopping: {e}")
            time.sleep(0.5)
            self.root.quit()
            self.root.destroy()
            print("GUI closed cleanly.")


if __name__ == "__main__":
    root = tk.Tk()
    app = HamamatsuGUI(root)
    root.mainloop()
