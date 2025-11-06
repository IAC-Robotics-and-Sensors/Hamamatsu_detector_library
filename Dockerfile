# ===============================
# Hamamatsu Detector Controller
# ===============================

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for GUI, USB, and plotting
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-tk \
    python3-dev \
    libusb-1.0-0-dev \
    libopenblas-dev \
    libx11-dev \
    tk-dev \
    uhubctl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy your project files
COPY hamamatsu_controller.py hamamatsu_gui.py hamamatsu_example_acquisition.py requirements.txt README.md ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Use TkAgg backend for Matplotlib
ENV MPLBACKEND=TKAgg

# Default command: start the GUI
CMD ["python", "hamamatsu_gui.py"]
