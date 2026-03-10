"""
Hamamatsu Detector GUI + Peak ID Integration
===========================================

PyQt6 + Matplotlib GUI for the HamamatsuController, extended with peak
identification from peakID/main.py.

Features:
 - Everything from hamamatsu_gui.py
 - Peak ID settings tab (runtime-configurable)
 - "Identify Peaks" button on Spectrum tab
 - Isotope guess table shown under spectrum
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from hamamatsu_controller import HamamatsuController
from c12137_comm import (
    RDMUSB_SUCCESS,
    EEPROM_COMP_LEVEL,
    EEPROM_ENERGY_LOWER,
    EEPROM_ENERGY_UPPER,
    EEPROM_CONVERT_USV,
)


THIS_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = THIS_DIR.parent
PEAKID_DIR = WORKSPACE_ROOT / "peakID"
PEAKID_MAIN_PATH = PEAKID_DIR / "main.py"


class MainWindow(QMainWindow):
    """Primary application window."""

    log_signal = pyqtSignal(str)
    peakid_result_signal = pyqtSignal(bool, object, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hamamatsu C12137 — Radiation Detector + Peak ID")
        self.resize(1200, 850)

        self.controller = HamamatsuController(verbose=True)
        self._acquiring = False

        self._peakid_busy = False

        # Calibration state
        self._cal_markers: list[dict] = []
        self._cal_poly: np.poly1d | None = None
        self._cal_applied = False
        self._pick_mode = False
        self._marker_lines: list = []

        self._build_ui()
        self._connect_signals()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)

        self.log_signal.connect(self._append_log)
        self.peakid_result_signal.connect(self._on_peakid_finished)

        self._set_controls_enabled(False)
        self._init_peakid_defaults()

    # ── UI construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setEnabled(False)
        toolbar.addWidget(self.btn_connect)
        toolbar.addWidget(self.btn_disconnect)
        toolbar.addStretch()
        self.lbl_status = QLabel("Disconnected")
        toolbar.addWidget(self.lbl_status)
        root.addLayout(toolbar)

        tabs = QTabWidget()
        root.addWidget(tabs)

        spectrum_tab = QWidget()
        tabs.addTab(spectrum_tab, "Spectrum")
        self._build_spectrum_tab(spectrum_tab)

        acq_tab = QWidget()
        tabs.addTab(acq_tab, "Acquisition")
        self._build_acquisition_tab(acq_tab)

        settings_tab = QWidget()
        tabs.addTab(settings_tab, "Settings")
        self._build_settings_tab(settings_tab)

        peakid_tab = QWidget()
        tabs.addTab(peakid_tab, "Peak ID Settings")
        self._build_peakid_tab(peakid_tab)

        cal_tab = QWidget()
        tabs.addTab(cal_tab, "Calibration")
        self._build_calibration_tab(cal_tab)

        eeprom_tab = QWidget()
        tabs.addTab(eeprom_tab, "EEPROM")
        self._build_eeprom_tab(eeprom_tab)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(120)
        root.addWidget(self.log_box)

        self.setStatusBar(QStatusBar())

    # -- Spectrum tab --------------------------------------------------------
    def _build_spectrum_tab(self, parent: QWidget):
        lay = QVBoxLayout(parent)

        self.fig = Figure(figsize=(9, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Channel")
        self.ax.set_ylabel("Counts")
        self.ax.set_title("Live Spectrum")
        self.canvas = FigureCanvas(self.fig)
        lay.addWidget(self.canvas)
        self._line = None

        ctrl = QHBoxLayout()
        self.btn_start = QPushButton("Start Acquisition")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_clear = QPushButton("Clear Spectrum")
        self.btn_save = QPushButton("Save Spectrum")
        self.chk_log_scale = QCheckBox("Log scale")
        self.btn_identify = QPushButton("Identify Peaks")
        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_stop)
        ctrl.addWidget(self.btn_clear)
        ctrl.addWidget(self.btn_save)
        ctrl.addWidget(self.chk_log_scale)
        ctrl.addWidget(self.btn_identify)
        ctrl.addStretch()

        self.lbl_temp = QLabel("Temp: — °C")
        self.lbl_counts = QLabel("Counts: 0")
        self.lbl_cps = QLabel("CPS: 0")
        self.lbl_elapsed = QLabel("Time: 0 s")
        ctrl.addWidget(self.lbl_temp)
        ctrl.addWidget(self.lbl_counts)
        ctrl.addWidget(self.lbl_cps)
        ctrl.addWidget(self.lbl_elapsed)

        lay.addLayout(ctrl)

        peak_grp = QGroupBox("Peak Identification")
        peak_lay = QVBoxLayout(peak_grp)
        self.lbl_peakid_status = QLabel("Peak ID: not run")
        peak_lay.addWidget(self.lbl_peakid_status)

        self.peak_table = QTableWidget(0, 6)
        self.peak_table.setHorizontalHeaderLabels(
            ["Isotope", "Energy (keV)", "RI (%)", "R² of fit", "CPS", "ACF"]
        )
        self.peak_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.peak_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        peak_lay.addWidget(self.peak_table)
        lay.addWidget(peak_grp)

    # -- Acquisition tab -----------------------------------------------------
    def _build_acquisition_tab(self, parent: QWidget):
        lay = QVBoxLayout(parent)

        grp_timed = QGroupBox("Timed Acquisition")
        g1 = QHBoxLayout(grp_timed)
        g1.addWidget(QLabel("Duration (s):"))
        self.spin_acq_duration = QSpinBox()
        self.spin_acq_duration.setRange(1, 100000)
        self.spin_acq_duration.setValue(10)
        self.spin_acq_duration.setSuffix(" s")
        g1.addWidget(self.spin_acq_duration)
        self.btn_acquire_fixed = QPushButton("Acquire Fixed Spectrum")
        g1.addWidget(self.btn_acquire_fixed)
        g1.addStretch()
        lay.addWidget(grp_timed)

        grp_log = QGroupBox("Periodic Logging")
        g2 = QHBoxLayout(grp_log)
        g2.addWidget(QLabel("Interval (s):"))
        self.spin_log_interval = QSpinBox()
        self.spin_log_interval.setRange(1, 100000)
        self.spin_log_interval.setValue(10)
        self.spin_log_interval.setSuffix(" s")
        g2.addWidget(self.spin_log_interval)
        g2.addWidget(QLabel("Total (s, 0 = continuous):"))
        self.spin_log_total = QSpinBox()
        self.spin_log_total.setRange(0, 1000000)
        self.spin_log_total.setValue(0)
        self.spin_log_total.setSuffix(" s")
        g2.addWidget(self.spin_log_total)
        self.btn_start_logging = QPushButton("Start Logging")
        self.btn_stop_logging = QPushButton("Stop Logging")
        g2.addWidget(self.btn_start_logging)
        g2.addWidget(self.btn_stop_logging)
        g2.addStretch()
        lay.addWidget(grp_log)

        lay.addStretch()

    # -- Settings tab --------------------------------------------------------
    def _build_settings_tab(self, parent: QWidget):
        lay = QVBoxLayout(parent)

        grp_th = QGroupBox("Gamma-Ray Energy Lower Limit (Threshold)")
        g1 = QHBoxLayout(grp_th)
        self.btn_get_threshold = QPushButton("Get")
        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(0, 4095)
        self.spin_threshold.setSuffix(" (index)")
        self.btn_set_threshold = QPushButton("Set")
        g1.addWidget(self.btn_get_threshold)
        g1.addWidget(QLabel("Value:"))
        g1.addWidget(self.spin_threshold)
        g1.addWidget(self.btn_set_threshold)
        g1.addStretch()
        lay.addWidget(grp_th)

        grp_lim = QGroupBox("Effective Energy Range for Air Dose (keV)")
        g2 = QHBoxLayout(grp_lim)
        self.btn_get_lower = QPushButton("Get Lower")
        self.spin_lower = QSpinBox()
        self.spin_lower.setRange(30, 2000)
        self.spin_lower.setValue(30)
        self.spin_lower.setSuffix(" keV")
        self.btn_set_lower = QPushButton("Set Lower")
        g2.addWidget(self.btn_get_lower)
        g2.addWidget(self.spin_lower)
        g2.addWidget(self.btn_set_lower)
        g2.addSpacing(30)
        self.btn_get_upper = QPushButton("Get Upper")
        self.spin_upper = QSpinBox()
        self.spin_upper.setRange(30, 2000)
        self.spin_upper.setValue(2000)
        self.spin_upper.setSuffix(" keV")
        self.btn_set_upper = QPushButton("Set Upper")
        g2.addWidget(self.btn_get_upper)
        g2.addWidget(self.spin_upper)
        g2.addWidget(self.btn_set_upper)
        g2.addStretch()
        lay.addWidget(grp_lim)

        grp_misc = QGroupBox("Miscellaneous")
        g3 = QHBoxLayout(grp_misc)
        self.btn_int_temp = QPushButton("Read Internal Temp")
        self.btn_reset_module = QPushButton("Reset Module")
        self.btn_reset_module.setStyleSheet("color: red;")
        g3.addWidget(self.btn_int_temp)
        g3.addWidget(self.btn_reset_module)
        g3.addStretch()
        lay.addWidget(grp_misc)

        lay.addStretch()

    # -- Peak ID settings tab ------------------------------------------------
    def _build_peakid_tab(self, parent: QWidget):
        lay = QVBoxLayout(parent)

        grp_modes = QGroupBox("Peak ID Modes")
        gm = QHBoxLayout(grp_modes)
        self.chk_pid_mariscotti = QCheckBox("Use Mariscotti peak finding")
        self.chk_pid_mariscotti.setChecked(False)
        self.chk_pid_voigt = QCheckBox("Use Voigt fit")
        self.chk_pid_voigt.setChecked(False)
        self.chk_pid_smooth = QCheckBox("Smooth spectrum")
        self.chk_pid_smooth.setChecked(True)
        self.chk_pid_effcorr = QCheckBox("Efficiency correction")
        self.chk_pid_effcorr.setChecked(False)
        gm.addWidget(self.chk_pid_mariscotti)
        gm.addWidget(self.chk_pid_voigt)
        gm.addWidget(self.chk_pid_smooth)
        gm.addWidget(self.chk_pid_effcorr)
        gm.addStretch()
        lay.addWidget(grp_modes)

        grp_peak = QGroupBox("Peak Finding / Fitting")
        gp = QHBoxLayout(grp_peak)
        gp.addWidget(QLabel("min FWHM"))
        self.spin_pid_min_fwhm = QSpinBox()
        self.spin_pid_min_fwhm.setRange(1, 500)
        gp.addWidget(self.spin_pid_min_fwhm)

        gp.addWidget(QLabel("max FWHM"))
        self.spin_pid_max_fwhm = QSpinBox()
        self.spin_pid_max_fwhm.setRange(2, 1000)
        gp.addWidget(self.spin_pid_max_fwhm)

        gp.addWidget(QLabel("step"))
        self.spin_pid_fwhm_step = QSpinBox()
        self.spin_pid_fwhm_step.setRange(1, 100)
        gp.addWidget(self.spin_pid_fwhm_step)

        gp.addWidget(QLabel("confidence"))
        self.spin_pid_conf = QDoubleSpinBox()
        self.spin_pid_conf.setRange(0.0, 1000.0)
        self.spin_pid_conf.setDecimals(3)
        gp.addWidget(self.spin_pid_conf)

        gp.addWidget(QLabel("intensity threshold"))
        self.edit_pid_int_thr = QLineEdit()
        self.edit_pid_int_thr.setMaximumWidth(110)
        gp.addWidget(self.edit_pid_int_thr)

        gp.addWidget(QLabel("iters"))
        self.spin_pid_iters = QSpinBox()
        self.spin_pid_iters.setRange(1, 20)
        gp.addWidget(self.spin_pid_iters)

        gp.addWidget(QLabel("base width"))
        self.spin_pid_base_width = QSpinBox()
        self.spin_pid_base_width.setRange(2, 100)
        gp.addWidget(self.spin_pid_base_width)
        gp.addStretch()
        lay.addWidget(grp_peak)

        grp_disc = QGroupBox("Discriminators / Smoothing")
        gd = QHBoxLayout(grp_disc)
        gd.addWidget(QLabel("LLD"))
        self.spin_pid_lld = QSpinBox()
        self.spin_pid_lld.setRange(0, 4095)
        gd.addWidget(self.spin_pid_lld)

        gd.addWidget(QLabel("HLD"))
        self.spin_pid_hld = QSpinBox()
        self.spin_pid_hld.setRange(0, 4095)
        gd.addWidget(self.spin_pid_hld)

        gd.addWidget(QLabel("smooth window"))
        self.spin_pid_smooth_window = QSpinBox()
        self.spin_pid_smooth_window.setRange(3, 1001)
        gd.addWidget(self.spin_pid_smooth_window)

        gd.addWidget(QLabel("smooth order"))
        self.spin_pid_smooth_order = QSpinBox()
        self.spin_pid_smooth_order.setRange(1, 10)
        gd.addWidget(self.spin_pid_smooth_order)

        gd.addWidget(QLabel("Spectrum time (s)"))
        self.spin_pid_time = QDoubleSpinBox()
        self.spin_pid_time.setRange(0.0, 1e9)
        self.spin_pid_time.setDecimals(3)
        gd.addWidget(self.spin_pid_time)
        gd.addStretch()
        lay.addWidget(grp_disc)

        grp_bg = QGroupBox("Background Subtraction")
        gb = QHBoxLayout(grp_bg)
        gb.addWidget(QLabel("Type"))
        self.cmb_pid_bg = QComboBox()
        self.cmb_pid_bg.addItems(["none", "rolling_ball", "linear"])
        gb.addWidget(self.cmb_pid_bg)

        gb.addWidget(QLabel("Ball radius"))
        self.spin_pid_ball = QSpinBox()
        self.spin_pid_ball.setRange(1, 5000)
        gb.addWidget(self.spin_pid_ball)

        gb.addWidget(QLabel("Gradient"))
        self.spin_pid_grad = QDoubleSpinBox()
        self.spin_pid_grad.setRange(-1e4, 1e4)
        self.spin_pid_grad.setDecimals(4)
        gb.addWidget(self.spin_pid_grad)

        gb.addWidget(QLabel("Grad type"))
        self.cmb_pid_gradtype = QComboBox()
        self.cmb_pid_gradtype.addItems(["sqrt", "linear"])
        gb.addWidget(self.cmb_pid_gradtype)
        gb.addStretch()
        lay.addWidget(grp_bg)

        grp_id = QGroupBox("Isotope ID")
        gi = QHBoxLayout(grp_id)
        gi.addWidget(QLabel("Energy window (keV)"))
        self.spin_pid_energy_window = QSpinBox()
        self.spin_pid_energy_window.setRange(1, 200)
        gi.addWidget(self.spin_pid_energy_window)

        gi.addWidget(QLabel("Category"))
        self.cmb_pid_category = QComboBox()
        self.cmb_pid_category.addItems([
            "All",
            "Nuclear Power Plant",
            "Homeland Security",
            "Early Nuclear Accident",
            "Late-Time Nuclear Accident",
            "Spent Fuel",
            "CTBT OSI",
            "Laboratory Miscellaneous",
        ])
        gi.addWidget(self.cmb_pid_category)

        gi.addWidget(QLabel("Common isotopes (comma-separated)"))
        self.edit_pid_common = QLineEdit()
        gi.addWidget(self.edit_pid_common)
        lay.addWidget(grp_id)

        grp_cal = QGroupBox("Channel → Energy Defaults")
        gc = QHBoxLayout(grp_cal)
        gc.addWidget(QLabel("M"))
        self.spin_pid_M = QDoubleSpinBox()
        self.spin_pid_M.setRange(-1e5, 1e5)
        self.spin_pid_M.setDecimals(8)
        gc.addWidget(self.spin_pid_M)

        gc.addWidget(QLabel("c"))
        self.spin_pid_c = QDoubleSpinBox()
        self.spin_pid_c.setRange(-1e6, 1e6)
        self.spin_pid_c.setDecimals(8)
        gc.addWidget(self.spin_pid_c)

        gc.addStretch()
        lay.addWidget(grp_cal)

        hint = QLabel(
            "These settings are applied at runtime when you press Identify Peaks."
        )
        lay.addWidget(hint)
        lay.addStretch()

    # -- Calibration tab ------------------------------------------------------
    def _build_calibration_tab(self, parent: QWidget):
        lay = QVBoxLayout(parent)

        grp_pick = QGroupBox("Marker Placement")
        gp = QHBoxLayout(grp_pick)
        self.chk_pick_mode = QCheckBox("Enable marker picking (click on spectrum)")
        gp.addWidget(self.chk_pick_mode)
        gp.addStretch()
        lay.addWidget(grp_pick)

        grp_table = QGroupBox("Calibration Points")
        gt = QVBoxLayout(grp_table)
        self.cal_table = QTableWidget(0, 2)
        self.cal_table.setHorizontalHeaderLabels(["Channel", "Energy (keV)"])
        self.cal_table.horizontalHeader().setStretchLastSection(True)
        self.cal_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.cal_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        gt.addWidget(self.cal_table)

        tbl_btns = QHBoxLayout()
        self.btn_remove_marker = QPushButton("Remove Selected")
        self.btn_clear_markers = QPushButton("Clear Markers")
        self.btn_clear_all_cal = QPushButton("Clear All (incl. calibration)")
        self.btn_clear_all_cal.setStyleSheet("color: red;")
        tbl_btns.addWidget(self.btn_remove_marker)
        tbl_btns.addWidget(self.btn_clear_markers)
        tbl_btns.addWidget(self.btn_clear_all_cal)
        tbl_btns.addStretch()
        gt.addLayout(tbl_btns)
        lay.addWidget(grp_table)

        grp_fit = QGroupBox("Polynomial Fit")
        gf = QHBoxLayout(grp_fit)
        gf.addWidget(QLabel("Polynomial order:"))
        self.spin_poly_order = QSpinBox()
        self.spin_poly_order.setRange(1, 5)
        self.spin_poly_order.setValue(1)
        gf.addWidget(self.spin_poly_order)
        self.btn_fit_cal = QPushButton("Fit")
        gf.addWidget(self.btn_fit_cal)
        self.chk_apply_cal = QCheckBox("Apply calibration to plot")
        gf.addWidget(self.chk_apply_cal)
        gf.addStretch()
        lay.addWidget(grp_fit)

        grp_coeff = QGroupBox("Calibration Coefficients")
        gc = QVBoxLayout(grp_coeff)
        self.lbl_cal_coeffs = QLabel("No calibration computed yet.")
        self.lbl_cal_coeffs.setWordWrap(True)
        gc.addWidget(self.lbl_cal_coeffs)
        lay.addWidget(grp_coeff)

        grp_io = QGroupBox("Save / Load")
        gio = QHBoxLayout(grp_io)
        self.btn_save_cal = QPushButton("Save Calibration…")
        self.btn_load_cal = QPushButton("Load Calibration…")
        gio.addWidget(self.btn_save_cal)
        gio.addWidget(self.btn_load_cal)
        gio.addStretch()
        lay.addWidget(grp_io)

        lay.addStretch()

    # -- EEPROM tab ----------------------------------------------------------
    def _build_eeprom_tab(self, parent: QWidget):
        lay = QVBoxLayout(parent)
        grp = QGroupBox("Read EEPROM")
        g = QHBoxLayout(grp)
        self.cmb_eeprom = QComboBox()
        self.cmb_eeprom.addItem("0x0A — Comparator threshold", EEPROM_COMP_LEVEL)
        self.cmb_eeprom.addItem("0x0C — Energy lower limit", EEPROM_ENERGY_LOWER)
        self.cmb_eeprom.addItem("0x0E — Energy upper limit", EEPROM_ENERGY_UPPER)
        self.cmb_eeprom.addItem("0x10 — Calibration coeff", EEPROM_CONVERT_USV)
        self.btn_read_eeprom = QPushButton("Read")
        g.addWidget(self.cmb_eeprom)
        g.addWidget(self.btn_read_eeprom)
        g.addStretch()
        lay.addWidget(grp)
        lay.addStretch()

    # ── signal wiring ────────────────────────────────────────────────────────
    def _connect_signals(self):
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect.clicked.connect(self._on_disconnect)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_save.clicked.connect(self._on_save)
        self.chk_log_scale.stateChanged.connect(self._on_log_scale)
        self.btn_identify.clicked.connect(self._on_identify_peaks)

        self.btn_acquire_fixed.clicked.connect(self._on_acquire_fixed)
        self.btn_start_logging.clicked.connect(self._on_start_logging)
        self.btn_stop_logging.clicked.connect(self._on_stop_logging)

        self.btn_get_threshold.clicked.connect(self._on_get_threshold)
        self.btn_set_threshold.clicked.connect(self._on_set_threshold)
        self.btn_get_lower.clicked.connect(self._on_get_lower)
        self.btn_set_lower.clicked.connect(self._on_set_lower)
        self.btn_get_upper.clicked.connect(self._on_get_upper)
        self.btn_set_upper.clicked.connect(self._on_set_upper)
        self.btn_int_temp.clicked.connect(self._on_get_int_temp)
        self.btn_reset_module.clicked.connect(self._on_reset_module)
        self.btn_read_eeprom.clicked.connect(self._on_read_eeprom)

        self.chk_pick_mode.stateChanged.connect(self._on_pick_mode_toggled)
        self.btn_remove_marker.clicked.connect(self._on_remove_marker)
        self.btn_clear_markers.clicked.connect(self._on_clear_markers)
        self.btn_clear_all_cal.clicked.connect(self._on_clear_all_cal)
        self.btn_fit_cal.clicked.connect(self._on_fit_calibration)
        self.chk_apply_cal.stateChanged.connect(self._on_apply_calibration)
        self.btn_save_cal.clicked.connect(self._on_save_calibration)
        self.btn_load_cal.clicked.connect(self._on_load_calibration)
        self.cal_table.cellChanged.connect(self._on_cal_table_edited)

        self.canvas.mpl_connect("button_press_event", self._on_canvas_click)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _set_controls_enabled(self, on: bool):
        for w in (
            self.btn_start,
            self.btn_stop,
            self.btn_clear,
            self.btn_save,
            self.btn_identify,
            self.btn_acquire_fixed,
            self.btn_start_logging,
            self.btn_stop_logging,
            self.btn_get_threshold,
            self.btn_set_threshold,
            self.btn_get_lower,
            self.btn_set_lower,
            self.btn_get_upper,
            self.btn_set_upper,
            self.btn_int_temp,
            self.btn_reset_module,
            self.btn_read_eeprom,
        ):
            w.setEnabled(on)

    def _log(self, msg: str):
        self.log_signal.emit(msg)

    @pyqtSlot(str)
    def _append_log(self, msg: str):
        self.log_box.append(msg)

    def _init_peakid_defaults(self):
        self.spin_pid_min_fwhm.setValue(20)
        self.spin_pid_max_fwhm.setValue(150)
        self.spin_pid_fwhm_step.setValue(5)
        self.spin_pid_conf.setValue(1.0)
        self.edit_pid_int_thr.setText("1e-9")
        self.spin_pid_lld.setValue(80)
        self.spin_pid_hld.setValue(4080)
        self.spin_pid_iters.setValue(2)
        self.spin_pid_base_width.setValue(10)
        self.spin_pid_smooth_window.setValue(31)
        self.spin_pid_smooth_order.setValue(2)
        self.spin_pid_time.setValue(0.0)

        self.cmb_pid_bg.setCurrentText("none")
        self.spin_pid_ball.setValue(50)
        self.spin_pid_grad.setValue(10.0)
        self.cmb_pid_gradtype.setCurrentText("sqrt")

        self.spin_pid_energy_window.setValue(15)
        self.cmb_pid_category.setCurrentText("All")
        self.edit_pid_common.setText(
            "Cs-137, Co-60, U233, Cf252, Cs-134, Bi-207, Bi-214, Pb-207, Pb-210, Pb-214, Sr-85, Ag-110m"
        )

        self.spin_pid_M.setValue(1.0)
        self.spin_pid_c.setValue(0.0)

    def _collect_peakid_settings(self) -> dict:
        try:
            intensity_threshold = float(self.edit_pid_int_thr.text().strip())
        except ValueError:
            intensity_threshold = 1e-9

        common_isotopes = [
            tok.strip()
            for tok in self.edit_pid_common.text().split(",")
            if tok.strip()
        ]

        smooth_window = self.spin_pid_smooth_window.value()
        if smooth_window % 2 == 0:
            smooth_window += 1

        return {
            "M": float(self.spin_pid_M.value()),
            "c": float(self.spin_pid_c.value()),
            "min_FWHM": int(self.spin_pid_min_fwhm.value()),
            "max_FWHM": int(self.spin_pid_max_fwhm.value()),
            "FWHM_step": int(self.spin_pid_fwhm_step.value()),
            "time": float(self.spin_pid_time.value()),
            "confidence": float(self.spin_pid_conf.value()),
            "intensity_threshold": float(intensity_threshold),
            "LLD": int(self.spin_pid_lld.value()),
            "HLD": int(self.spin_pid_hld.value()),
            "num_iters": int(self.spin_pid_iters.value()),
            "base_width": int(self.spin_pid_base_width.value()),
            "smooth_window": int(smooth_window),
            "smooth_order": int(self.spin_pid_smooth_order.value()),
            "background_subtraction": self.cmb_pid_bg.currentText(),
            "ballradius": int(self.spin_pid_ball.value()),
            "gradient": float(self.spin_pid_grad.value()),
            "gradtype": self.cmb_pid_gradtype.currentText(),
            "energy_window_size": int(self.spin_pid_energy_window.value()),
            "category": self.cmb_pid_category.currentText(),
            "common_isotopes": common_isotopes,
        }

    def _load_peakid_main_module(self):
        if not PEAKID_MAIN_PATH.exists():
            raise FileNotFoundError(f"Peak ID main.py not found at {PEAKID_MAIN_PATH}")

        spec = importlib.util.spec_from_file_location("peakid_main_module", str(PEAKID_MAIN_PATH))
        if spec is None or spec.loader is None:
            raise ImportError("Unable to load peakID main module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _apply_peakid_runtime_config(self, settings: dict):
        if str(PEAKID_DIR) not in sys.path:
            sys.path.insert(0, str(PEAKID_DIR))

        cfg = importlib.import_module("config")
        cfg = importlib.reload(cfg)

        for key, value in settings.items():
            setattr(cfg, key, value)

    def _populate_peak_table(self, df):
        self.peak_table.setRowCount(0)
        if df is None or len(df) == 0:
            return

        columns = list(df.columns)
        expected = ["Isotope", "Energy (keV)", "RI (%)", "\u0305R^2 of fit", "CPS", "ACF"]
        if len(columns) >= 6:
            self.peak_table.setColumnCount(6)
        else:
            self.peak_table.setColumnCount(len(columns))

        self.peak_table.setHorizontalHeaderLabels(expected[: self.peak_table.columnCount()])

        self.peak_table.setRowCount(len(df))
        for r in range(len(df)):
            row_vals = df.iloc[r].tolist()
            for c in range(min(self.peak_table.columnCount(), len(row_vals))):
                item = QTableWidgetItem(str(row_vals[c]))
                self.peak_table.setItem(r, c, item)

    # ── Peak ID slots ────────────────────────────────────────────────────────
    def _on_identify_peaks(self):
        if self._peakid_busy:
            return

        if not PEAKID_DIR.exists():
            QMessageBox.warning(self, "Peak ID", f"Peak ID directory not found:\n{PEAKID_DIR}")
            return

        if self._line is not None:
            spectrum = np.asarray(self._line.get_ydata(), dtype=float)
        else:
            spectrum, _, _, _, _ = self.controller.get_spectrum()
            spectrum = np.asarray(spectrum, dtype=float)

        if spectrum.size == 0 or np.all(spectrum <= 0):
            QMessageBox.warning(self, "Peak ID", "No spectrum data available for identification.")
            return

        self._peakid_busy = True
        self.btn_identify.setEnabled(False)
        self.lbl_peakid_status.setText("Peak ID: running…")

        settings = self._collect_peakid_settings()
        options = {
            "mariscotti": self.chk_pid_mariscotti.isChecked(),
            "useVoigt": self.chk_pid_voigt.isChecked(),
            "smooth": self.chk_pid_smooth.isChecked(),
            "efficiency_correction": self.chk_pid_effcorr.isChecked(),
        }

        self._log("Running peak identification...")
        threading.Thread(
            target=self._run_peakid_worker,
            args=(spectrum, settings, options),
            daemon=True,
        ).start()

    def _run_peakid_worker(self, spectrum: np.ndarray, settings: dict, options: dict):
        original_cwd = os.getcwd()
        emitted = False
        try:
            self._apply_peakid_runtime_config(settings)
            peakid_main = self._load_peakid_main_module()
            os.chdir(str(PEAKID_DIR))

            df, _, _, _ = peakid_main.main(
                spectrum,
                mariscotti=options["mariscotti"],
                identification=True,
                efficiency_correction=options["efficiency_correction"],
                useVoigt=options["useVoigt"],
                smooth=options["smooth"],
            )
            self.peakid_result_signal.emit(True, df, "Peak identification complete.")
            emitted = True
        except SystemExit:
            self.peakid_result_signal.emit(False, None, "Peak identification completed: no peaks found.")
            emitted = True
        except Exception as exc:
            self.peakid_result_signal.emit(False, None, f"Peak ID failed: {exc}")
            emitted = True
        except BaseException as exc:
            self.peakid_result_signal.emit(False, None, f"Peak ID failed: {exc}")
            emitted = True
        finally:
            os.chdir(original_cwd)
            if not emitted:
                self.peakid_result_signal.emit(False, None, "Peak ID ended unexpectedly.")

    @pyqtSlot(bool, object, str)
    def _on_peakid_finished(self, ok: bool, df, message: str):
        self._peakid_busy = False
        self.btn_identify.setEnabled(True)

        if ok:
            self._populate_peak_table(df)
            n = 0 if df is None else len(df)
            self.lbl_peakid_status.setText(f"Peak ID: complete ({n} rows)")
            self._log(message)
        else:
            self.lbl_peakid_status.setText("Peak ID: failed")
            self._log(message)
            QMessageBox.warning(self, "Peak ID", message)

    # ── calibration helpers ──────────────────────────────────────────────────
    def _sync_table_to_markers(self):
        self.cal_table.blockSignals(True)
        self.cal_table.setRowCount(len(self._cal_markers))
        for row, m in enumerate(self._cal_markers):
            ch_item = QTableWidgetItem(str(m["channel"]))
            ch_item.setFlags(ch_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.cal_table.setItem(row, 0, ch_item)
            en_item = QTableWidgetItem(f"{m['energy']:.2f}")
            self.cal_table.setItem(row, 1, en_item)
        self.cal_table.blockSignals(False)

    def _update_marker_lines(self):
        for ln in self._marker_lines:
            ln.remove()
        self._marker_lines.clear()

        for m in self._cal_markers:
            xval = m["channel"]
            if self._cal_applied and self._cal_poly is not None:
                xval = float(self._cal_poly(m["channel"]))
            ln = self.ax.axvline(
                xval, color="red", linestyle="--", linewidth=0.9, alpha=0.7
            )
            self._marker_lines.append(ln)
        self.canvas.draw_idle()

    def _rebuild_plot_xaxis(self):
        if self._line is None:
            return
        n = len(self._line.get_ydata())
        if self._cal_applied and self._cal_poly is not None:
            x = self._cal_poly(np.arange(n))
            self.ax.set_xlabel("Energy (keV)")
        else:
            x = np.arange(n, dtype=float)
            self.ax.set_xlabel("Channel")
        self._line.set_xdata(x)
        self.ax.set_xlim(x.min(), x.max())
        self._update_marker_lines()
        self.canvas.draw_idle()

    # ── calibration slots ────────────────────────────────────────────────────
    def _on_pick_mode_toggled(self, state):
        self._pick_mode = bool(state)
        if self._pick_mode:
            self._log("Marker pick mode ON — click on spectrum to place markers.")
        else:
            self._log("Marker pick mode OFF.")

    def _on_canvas_click(self, event):
        if not self._pick_mode:
            return
        if event.inaxes != self.ax:
            return
        if event.button != 1:
            return

        if self._cal_applied and self._cal_poly is not None:
            n = 4096
            energies = self._cal_poly(np.arange(n))
            channel = int(np.argmin(np.abs(energies - event.xdata)))
        else:
            channel = int(round(event.xdata))
        channel = max(0, min(channel, 4095))

        if any(m["channel"] == channel for m in self._cal_markers):
            self._log(f"Channel {channel} already has a marker.")
            return

        self._cal_markers.append({"channel": channel, "energy": 0.0})
        self._cal_markers.sort(key=lambda m: m["channel"])
        self._sync_table_to_markers()
        self._update_marker_lines()
        self._log(f"Marker added at channel {channel}. Enter its energy in the table.")

    def _on_cal_table_edited(self, row, col):
        if col != 1:
            return
        item = self.cal_table.item(row, col)
        if item is None:
            return
        try:
            energy = float(item.text())
        except ValueError:
            self._log(f"Invalid energy value: {item.text()!r}")
            return
        if 0 <= row < len(self._cal_markers):
            self._cal_markers[row]["energy"] = energy

    def _on_remove_marker(self):
        rows = sorted(
            {idx.row() for idx in self.cal_table.selectedIndexes()}, reverse=True
        )
        if not rows:
            return
        for r in rows:
            if 0 <= r < len(self._cal_markers):
                del self._cal_markers[r]
        self._sync_table_to_markers()
        self._update_marker_lines()
        self._log(f"Removed {len(rows)} marker(s).")

    def _on_clear_markers(self):
        self._cal_markers.clear()
        self._sync_table_to_markers()
        self._update_marker_lines()
        self._log("Markers cleared (calibration retained).")

    def _on_clear_all_cal(self):
        self._cal_markers.clear()
        self._cal_poly = None
        self._cal_applied = False
        self.chk_apply_cal.setChecked(False)
        self.lbl_cal_coeffs.setText("No calibration computed yet.")
        self._sync_table_to_markers()
        self._update_marker_lines()
        self._rebuild_plot_xaxis()
        self._log("All calibration markers and fit cleared.")

    def _on_fit_calibration(self):
        order = self.spin_poly_order.value()
        pts = [
            (m["channel"], m["energy"])
            for m in self._cal_markers
            if m["energy"] != 0.0
        ]
        if len(pts) < order + 1:
            QMessageBox.warning(
                self,
                "Calibration",
                f"Need at least {order + 1} points with non-zero energies for a "
                f"degree-{order} polynomial (have {len(pts)}).",
            )
            return
        channels = np.array([p[0] for p in pts], dtype=float)
        energies = np.array([p[1] for p in pts], dtype=float)
        coeffs = np.polyfit(channels, energies, order)
        self._cal_poly = np.poly1d(coeffs)

        terms = []
        for i, c in enumerate(coeffs):
            power = order - i
            if power == 0:
                terms.append(f"{c:+.6g}")
            elif power == 1:
                terms.append(f"{c:+.6g}·ch")
            else:
                terms.append(f"{c:+.6g}·ch^{power}")
        eqn = "E(ch) = " + " ".join(terms)
        self.lbl_cal_coeffs.setText(eqn)
        self._log(f"Calibration fit (order {order}): {eqn}")

        if self._cal_applied:
            self._rebuild_plot_xaxis()

    def _on_apply_calibration(self, state):
        if state and self._cal_poly is None:
            QMessageBox.warning(self, "Calibration", "Compute a fit first.")
            self.chk_apply_cal.setChecked(False)
            return
        self._cal_applied = bool(state)
        self._rebuild_plot_xaxis()
        if self._cal_applied:
            self._log("Calibration applied — x-axis now shows energy (keV).")
        else:
            self._log("Calibration removed — x-axis shows channels.")

    def _on_save_calibration(self):
        default_name = f"calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Calibration", default_name, "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        data = {
            "markers": self._cal_markers,
            "poly_order": self.spin_poly_order.value(),
            "coefficients": list(self._cal_poly.coeffs) if self._cal_poly else None,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self._log(f"Calibration saved to {path}")

    def _on_load_calibration(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Calibration", "", "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        with open(path) as f:
            data = json.load(f)
        self._cal_markers = data.get("markers", [])
        order = data.get("poly_order", 1)
        self.spin_poly_order.setValue(order)
        coeffs = data.get("coefficients")
        if coeffs is not None:
            self._cal_poly = np.poly1d(coeffs)
            deg = len(coeffs) - 1
            terms = []
            for i, c in enumerate(coeffs):
                power = deg - i
                if power == 0:
                    terms.append(f"{c:+.6g}")
                elif power == 1:
                    terms.append(f"{c:+.6g}·ch")
                else:
                    terms.append(f"{c:+.6g}·ch^{power}")
            self.lbl_cal_coeffs.setText("E(ch) = " + " ".join(terms))
        else:
            self._cal_poly = None
            self.lbl_cal_coeffs.setText("No calibration computed yet.")
        self._sync_table_to_markers()
        self._update_marker_lines()
        self._log(f"Calibration loaded from {path}")

    # ── connection slots ─────────────────────────────────────────────────────
    def _on_connect(self):
        if self.controller.connect():
            self.lbl_status.setText("Connected")
            self.btn_connect.setEnabled(False)
            self.btn_disconnect.setEnabled(True)
            self._set_controls_enabled(True)
            self.btn_stop.setEnabled(False)
            self._log("Device connected successfully.")
        else:
            QMessageBox.warning(self, "Connection", "Device not found. Check USB connection.")

    def _on_disconnect(self):
        if self._acquiring:
            self._on_stop()
        self.controller.disconnect()
        self.lbl_status.setText("Disconnected")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self._set_controls_enabled(False)
        self._log("Device disconnected.")

    # ── acquisition slots ────────────────────────────────────────────────────
    def _on_start(self):
        if not self.controller.is_connected:
            return
        self.controller.start()
        self._acquiring = True
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._timer.start(100)
        self._log("Acquisition started.")

    def _on_stop(self):
        self.controller.stop()
        self._acquiring = False
        self._timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._log("Acquisition stopped.")

    def _on_clear(self):
        self.controller.reset()
        self._log("Spectrum cleared.")

    def _on_save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Spectrum", "", "Text Files (*.txt);;All Files (*)")
        if path:
            spec, elapsed, _, _, _ = self.controller.get_spectrum()
            np.savetxt(path, spec)
            self._log(f"Spectrum ({elapsed:.1f}s) saved to {path}")

    def _on_log_scale(self, state):
        self.ax.set_yscale("log" if state else "linear")
        self.canvas.draw_idle()

    # ── timed acquisition ────────────────────────────────────────────────────
    def _on_acquire_fixed(self):
        duration = self.spin_acq_duration.value()
        path, _ = QFileDialog.getSaveFileName(self, "Save Acquired Spectrum", "", "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        self.controller.reset()
        self._log(f"Acquiring for {duration}s...")

        def _worker():
            _, elapsed = self.controller.acquire_spectrum_for_duration(duration, path)
            self._log(f"Timed acquisition complete ({elapsed:.1f}s). Saved to {path}")

        threading.Thread(target=_worker, daemon=True).start()

    # ── logging ──────────────────────────────────────────────────────────────
    def _on_start_logging(self):
        interval = self.spin_log_interval.value()
        total = self.spin_log_total.value()
        path, _ = QFileDialog.getSaveFileName(self, "Log Base Filename", "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        self.controller.reset()
        self.controller.start_periodic_logging(path, interval, total)
        self._log(f"Logging started ({interval}s interval)")

    def _on_stop_logging(self):
        self.controller.stop_periodic_logging()
        self._log("Logging stopped.")

    # ── periodic refresh ─────────────────────────────────────────────────────
    def _refresh(self):
        if not self._acquiring:
            return
        spec, elapsed, cps, temp, _ = self.controller.get_spectrum()
        total_counts = int(spec.sum())

        self.lbl_temp.setText(f"Temp: {temp:.1f} °C")
        self.lbl_counts.setText(f"Counts: {total_counts}")
        self.lbl_cps.setText(f"CPS: {cps:.1f}")
        self.lbl_elapsed.setText(f"Time: {elapsed:.1f} s")

        channels = np.arange(len(spec))
        if self._cal_applied and self._cal_poly is not None:
            x = self._cal_poly(channels)
        else:
            x = channels.astype(float)

        if self._line is None:
            self._line, = self.ax.plot(x, spec, linewidth=0.8)
            self.ax.set_xlim(x.min(), x.max())
        else:
            self._line.set_xdata(x)
            self._line.set_ydata(spec)
            self.ax.set_xlim(x.min(), x.max())

        ymax = max(spec.max(), 1)
        if self.chk_log_scale.isChecked():
            self.ax.set_ylim(0.5, ymax * 2)
        else:
            self.ax.set_ylim(0, ymax * 1.1)

        self._update_marker_lines()
        self.canvas.draw_idle()

    # ── settings slots ───────────────────────────────────────────────────────
    def _on_get_threshold(self):
        status, idx = self.controller.get_energy_threshold()
        if status == RDMUSB_SUCCESS:
            self.spin_threshold.setValue(idx)
            self._log(f"Threshold index = {idx}")
        else:
            self._log("Failed to read energy threshold.")

    def _on_set_threshold(self):
        idx = self.spin_threshold.value()
        status = self.controller.set_energy_threshold(idx)
        if status == RDMUSB_SUCCESS:
            self._log(f"Threshold set to index {idx}")
        else:
            self._log("Failed to set energy threshold.")

    def _on_get_lower(self):
        status, val = self.controller.get_radiation_limit(0)
        if status == RDMUSB_SUCCESS:
            self.spin_lower.setValue(val)
            self._log(f"Lower limit = {val} keV")
        else:
            self._log("Failed to read lower limit.")

    def _on_set_lower(self):
        val = self.spin_lower.value()
        status = self.controller.set_radiation_limit(0, val)
        if status == RDMUSB_SUCCESS:
            self._log(f"Lower limit set to {val} keV")
        else:
            self._log("Failed to set lower limit.")

    def _on_get_upper(self):
        status, val = self.controller.get_radiation_limit(1)
        if status == RDMUSB_SUCCESS:
            self.spin_upper.setValue(val)
            self._log(f"Upper limit = {val} keV")
        else:
            self._log("Failed to read upper limit.")

    def _on_set_upper(self):
        val = self.spin_upper.value()
        status = self.controller.set_radiation_limit(1, val)
        if status == RDMUSB_SUCCESS:
            self._log(f"Upper limit set to {val} keV")
        else:
            self._log("Failed to set upper limit.")

    def _on_get_int_temp(self):
        status, celsius = self.controller.get_internal_temperature()
        if status == RDMUSB_SUCCESS:
            self._log(f"Internal temperature = {celsius:.1f} °C")
        else:
            self._log("Failed to read internal temperature.")

    def _on_reset_module(self):
        reply = QMessageBox.question(
            self, "Confirm", "Reset the detector module?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            status = self.controller.reset_module(0)
            if status == RDMUSB_SUCCESS:
                self._log("Module reset OK.")
            else:
                self._log("Module reset failed.")

    # ── EEPROM slot ──────────────────────────────────────────────────────────
    def _on_read_eeprom(self):
        addr = self.cmb_eeprom.currentData()
        status, val = self.controller.read_eeprom(addr)
        if status == RDMUSB_SUCCESS:
            labels = {
                EEPROM_COMP_LEVEL: "Comparator threshold",
                EEPROM_ENERGY_LOWER: "Energy lower limit",
                EEPROM_ENERGY_UPPER: "Energy upper limit",
                EEPROM_CONVERT_USV: "Calibration coefficient",
            }
            self._log(f"EEPROM [{addr:#04x}] {labels.get(addr, '?')} = {val}")
        else:
            self._log(f"EEPROM read failed (address {addr:#04x}).")

    # ── cleanup ──────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        if self._acquiring:
            self._on_stop()
        self.controller.disconnect()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
