#!/usr/bin/env python

from dynamixel_sdk import PortHandler, PacketHandler

# ====== SETTINGS ======
DEVICENAME = '/dev/ttyUSB0'    # Change this to your device's port
BAUDRATE = 57600               # Set to your motor's baud rate
PROTOCOL_VERSION = 2.0         # Use 1.0 if your motor uses protocol 1.0
DXL_ID = 0                     # Motor ID (change as needed)

# ====== INITIALIZE PORT AND PACKET HANDLERS ======
portHandler = PortHandler(DEVICENAME)
packetHandler = PacketHandler(PROTOCOL_VERSION)

# Open the port
if portHandler.openPort():
    print("Port opened successfully!")
else:
    print("Failed to open port. Check device path and permissions.")
    exit()

# Set the port baudrate
if portHandler.setBaudRate(BAUDRATE):
    print("Baudrate set successfully!")
else:
    print("Failed to set baudrate. Verify baud rate and connection.")
    exit()

# ====== READ A REGISTER ======
# For demonstration, we attempt to read the model number (2 bytes at address 0)
dxl_model_number, dxl_comm_result, dxl_error = packetHandler.read4ByteTxRx(portHandler, DXL_ID, 38)

if dxl_comm_result != 0:
    # Communication error (e.g., -3001 indicates a transmission failure)
    print("Communication failed:", packetHandler.getTxRxResult(dxl_comm_result))
elif dxl_error != 0:
    # Motor reported an error
    print("Motor error:", packetHandler.getRxPacketError(dxl_error))
else:
    print("Dynamixel model number:", dxl_model_number)

# Close the port after communication
portHandler.closePort()
