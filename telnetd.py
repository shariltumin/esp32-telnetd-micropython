# Daemon to start and manage Telnet server on ESP32

class Setup:
    def __init__(self):
        self.wifi_connected = False
        self.server_running = False

    def start_wifi(self):
        if not self.wifi_connected:
            import time
            import network
            import gc
            from config import wifi

            # Disable access point interface to save power and improve stability
            ap = network.WLAN(network.AP_IF)
            if ap.active():
                ap.active(False)

            # Initialize station interface
            wlan = network.WLAN(network.STA_IF)
            wlan.active(True)
            
            # ESP32 specific WiFi hardware optimizations
            try:
                wlan.config(pm=0)      # Disable power management for low latency
                wlan.config(txpower=20) # Max transmit power for reliable range
            except OSError:
                pass
            
            # Start connection
            wlan.connect(wifi[0], wifi[1])
            
            # Wait for connection with a robust 15-second timeout
            print("Connecting to Wi-Fi: {} ...".format(wifi[0]))
            attempts = 15
            while attempts > 0:
                time.sleep(1)
                if wlan.isconnected():
                    self.wifi_connected = True
                    print('Wi-Fi connected. IP:', wlan.ifconfig()[0])
                    break
                attempts -= 1
                
            if not self.wifi_connected:
                print('Wi-Fi connection timeout.')
            
            gc.collect()
        else:
            print('Wi-Fi already connected.')

    def start_server(self):
        if not self.server_running:
            import Telnet as tel
            import gc
            from config import appu

            if tel.repl(appu[0], appu[1]):
                self.server_running = True
                print('Telnet daemon successfully started.')
            else:
                print('Failed to start Telnet daemon.')
            
            gc.collect()
        else:
            print('Telnet daemon already running.')

# Run the initialization
app = Setup()
app.start_wifi()
app.start_server()

# Clean up module namespace to protect secrets and keep the namespace pristine
del Setup, app
