import os
import gc
import socket
import network
import errno
import micropython
from io import IOBase

# Preallocate emergency exception buffer for interrupts/callbacks
micropython.alloc_emergency_exception_buf(250)

# Constants
_PORT = micropython.const(23)
_SO_DUPTERM = micropython.const(20)

# Preallocated byte strings to avoid heap allocation during handshake and execution
_WELCOME_MSG = b'\r\n*************\r\n** Welcome **\r\n*************\r\n\r\n'
_MSG_USER = b'username: '
_MSG_PASS = b'password: '
_MSG_WRONG = b'\r\nWrong username and/or password.\r\n'
_MSG_OK = b'\r\nOK\r\nPress enter to continue. Write exit() to exit session.\r\n'
_MSG_BUSY = b'\r\nServer busy. Only one connection allowed.\r\n'

# Get default credentials
try:
    import config
    duser, dpwd = config.defu
except Exception:
    duser, dpwd = 'alibaba', '40robbers'

# Global state
srv = None   # Server listening socket
cln = None   # Active TelnetWrapper instance
ip = None    # Local IP address
NS = '1.1.1.1'
suser = ''
spwd = ''
auth = False
term = None  # Original terminal stream (UART)

class TelnetWrapper(IOBase):
    """Wraps client socket to filter Telnet options and provide non-blocking I/O for dupterm."""
    def __init__(self, socket_obj):
        self.socket = socket_obj
        self.discard_count = 0

    def readinto(self, b):
        if self.socket is None:
            return 0
        try:
            readbytes = 0
            for i in range(len(b)):
                byte = 0
                while byte == 0:
                    data = self.socket.read(1)
                    if data is None:
                        # Non-blocking: no data available
                        if readbytes == 0:
                            return None
                        return readbytes
                    if len(data) == 0:
                        # Client disconnected
                        exit()
                        return 0
                    
                    byte = data[0]
                    # Filter out Telnet IAC commands (0xFF)
                    if byte == 255:
                        self.discard_count = 2
                        byte = 0
                    elif self.discard_count > 0:
                        self.discard_count -= 1
                        byte = 0
                b[i] = byte
                readbytes += 1
            return readbytes
        except OSError as e:
            err = e.args[0] if e.args else None
            if err in (errno.EAGAIN, errno.EWOULDBLOCK):
                if readbytes == 0:
                    return None
                return readbytes
            exit()
            return 0

    def write(self, data):
        if self.socket is None:
            return 0
        try:
            written_total = 0
            while len(data) > 0:
                written = self.socket.write(data)
                if written is None:
                    continue
                if written == 0:
                    exit()
                    return 0
                written_total += written
                data = data[written:]
            return written_total
        except OSError as e:
            err = e.args[0] if e.args else None
            if err in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            exit()
            return 0

    def close(self):
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None

def _wifi():
    """Retrieve WiFi status and optionally enforce custom DNS."""
    global ip
    sta = network.WLAN(network.STA_IF)
    if sta.active() and sta.isconnected():
        ifc = sta.ifconfig()
        try:
            curr_ip, mask, gw, ns = ifc
            if ns != NS:
                # Set custom DNS server while preserving other parameters
                sta.ifconfig((curr_ip, mask, gw, NS))
            ip = curr_ip
        except Exception as e:
            print('DNS config error:', e)
            ip = ifc[0]
    else:
        print('No WiFi.')
        ip = None

def _cln_handler(srv_socket):
    """Callback triggered on socket option 20 when a new client connects."""
    global cln, auth, term
    
    try:
        cs, ca = srv_socket.accept()
    except OSError:
        return

    # Check if a client is already active
    if cln is not None:
        dp = os.dupterm(None)
        if dp is not None:
            # Another terminal is active, restore it and reject new connection
            os.dupterm(dp)
            try:
                cs.write(_MSG_BUSY)
                cs.close()
            except OSError:
                pass
            return
        else:
            # Existing client is dead, close it and clean up
            try:
                cln.close()
            except OSError:
                pass
            cln = None

    print('Serving:', ca)
    auth = False
    
    # Empty client's initial negotiation buffer using a preallocated buffer
    cs.setblocking(False)
    flush_buf = bytearray(32)
    try:
        while cs.readinto(flush_buf) is not None:
            pass
    except OSError:
        pass

    # Send Welcome Banner
    cs.setblocking(True)
    try:
        cs.write(_WELCOME_MSG)
    except OSError:
        try:
            cs.close()
        except OSError:
            pass
        return

    # Authentication Loop (3 attempts with non-blocking protection and timeout)
    cs.settimeout(15.0)
    for _ in range(3):
        try:
            cs.write(_MSG_USER)
            user_line = cs.readline()
            if not user_line:
                break
            user = user_line.strip().decode('utf-8', 'ignore')

            cs.write(_MSG_PASS)
            pwd_line = cs.readline()
            if not pwd_line:
                break
            pwd = pwd_line.strip().decode('utf-8', 'ignore')
        except (OSError, Exception):
            break

        if suser == user and spwd == pwd:
            auth = True
            try:
                cs.write(_MSG_OK)
            except OSError:
                auth = False
            break
        else:
            try:
                cs.write(_MSG_WRONG)
            except OSError:
                break

    # Restore non-blocking state for REPL
    cs.settimeout(None)
    cs.setblocking(False)

    if auth:
        # Disable line mode and local echoing on Telnet client
        try:
            cs.write(b'\xff\xfc\x22') # DONT LINEMODE
            cs.write(b'\xff\xfb\x01') # WILL ECHO
        except OSError:
            try:
                cs.close()
            except OSError:
                pass
            auth = False
            return

        # Notify REPL on incoming socket data
        if hasattr(os, 'dupterm_notify'):
            try:
                cs.setsockopt(socket.SOL_SOCKET, _SO_DUPTERM, os.dupterm_notify)
            except OSError:
                pass

        # Wrap client socket and duplicate REPL
        cln = TelnetWrapper(cs)
        term = os.dupterm(None)
        os.dupterm(cln)
        
        # Trigger GC to free up handshake memory
        gc.collect()
    else:
        print('Terminate:', ca[0])
        try:
            cs.close()
        except OSError:
            pass
        gc.collect()

def repl(user=duser, pwd=dpwd):
    """Initialize and start the Telnet server daemon."""
    global srv, ip, suser, spwd
    suser, spwd = user, pwd

    if not ip:
        _wifi()

    if ip:
        print("Starting Telnet daemon on", ip, "port 23")
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('0.0.0.0', _PORT))
            srv.listen(1)
            srv.setblocking(False)
            # Attach connection handler callback via socket option 20
            srv.setsockopt(socket.SOL_SOCKET, _SO_DUPTERM, _cln_handler)
            return True
        except Exception as e:
            print("Failed to start server:", e)
            if srv:
                try:
                    srv.close()
                except OSError:
                    pass
                srv = None
            return False
    else:
        print('No active connection. Daemon aborted.')
        return False

def exit():
    """Disconnect active client and restore original REPL."""
    global cln, auth, term
    auth = False
    
    if term is not None:
        try:
            os.dupterm(term)
        except Exception:
            pass
        term = None
        
    if cln is not None:
        try:
            cln.close()
        except Exception:
            pass
        cln = None
        
    print('Client disconnected.')
    gc.collect()

def stop():
    """Shut down both active client and listening server."""
    global srv
    exit()
    if srv is not None:
        try:
            srv.close()
        except Exception:
            pass
        srv = None
    print('Telnet daemon stopped.')
    gc.collect()

# Register exit and stop as builtins for direct REPL console access
try:
    import builtins
    builtins.exit = exit
    builtins.stop = stop
except Exception:
    pass
