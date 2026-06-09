#!/usr/bin/python3
import usb.core

devicelist=usb.core.find(find_all=True, idVendor=0x0661, idProduct=0x2917)

for device in devicelist:
	print(device.port_numbers)

