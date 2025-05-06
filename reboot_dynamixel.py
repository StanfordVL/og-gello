from dynamixel_sdk import *  # Uses Dynamixel SDK library

# Candidate device names
DEVICE_CANDIDATES = ['/dev/ttyUSB0', '/dev/ttyUSB1']
BAUDRATE = 2000000
PROTOCOL_VERSION = 2.0
ID_RANGE = range(0, 18)  # IDs 0–252

# Control table address for reboot (Protocol 2.0)
ADDR_REBOOT = 0x08

# Try each device until one works
portHandler = None
for dev in DEVICE_CANDIDATES:
    try:
        port = PortHandler(dev)
        if port.openPort() and port.setBaudRate(BAUDRATE):
            print(f"✅ Connected on {dev}")
            portHandler = port
            break
        else:
            print(f"❌ Could not open or set baud rate on {dev}")
    except:
        continue
# Exit if no valid port found
if portHandler is None:
    print("❌ No available serial port found.")
    exit()

# Initialize PacketHandler
packetHandler = PacketHandler(PROTOCOL_VERSION)

# Reboot each motor
for dxl_id in ID_RANGE:
    dxl_comm_result, dxl_error = packetHandler.reboot(portHandler, dxl_id)
    if dxl_comm_result == COMM_SUCCESS:
        print(f"🔁 Rebooted Dynamixel ID: {dxl_id}")
    else:
        print(f"⚠️ Could not reboot ID {dxl_id}: {packetHandler.getTxRxResult(dxl_comm_result)}")

# Close the port
portHandler.closePort()

