import serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    print("device       :", p.device)
    print("description  :", p.description)
    print("manufacturer :", p.manufacturer)
    print("product      :", p.product)
    print("vid/pid      :", p.vid, p.pid)
    print("serial       :", p.serial_number)
    print("hwid         :", p.hwid)
    print("-" * 60)