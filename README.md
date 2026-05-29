# MicroPython Event-Driven Telnet REPL Daemon for ESP32

This project implements a lightweight, event-driven Telnet daemon that redirects the MicroPython Read-Evaluate-Print Loop (REPL) to a remote Telnet client. The implementation utilizes a custom stream wrapper and native ESP32 socket callbacks (Socket Option 20) to handle asynchronous I/O with low RAM consumption and resilience against sudden disconnections.

---

## Technical Architecture & Core Snippets

### 1. Memory-Efficient I/O (Pre-allocation & Zero-Allocation)
To prevent heap fragmentation in memory-constrained environments, the system pre-allocates handshaking and negotiation buffers as module-level constants. Temporary objects are not created inside loops or during client handshake.

```python
# Module-level pre-allocated byte strings to avoid dynamic heap allocation
_WELCOME_MSG = b'\r\n*************\r\n** Welcome **\r\n*************\r\n\r\n'
_MSG_USER = b'username: '
_MSG_PASS = b'password: '
_MSG_WRONG = b'\r\nWrong username and/or password.\r\n'
_MSG_OK = b'\r\nOK\r\nPress enter to continue. Write exit() to exit session.\r\n'
_MSG_BUSY = b'\r\nServer busy. Only one connection allowed.\r\n'
```

Socket flushing is handled using a pre-allocated 32-byte array with `readinto` rather than repeatedly calling `read(1)`:
```python
flush_buf = bytearray(32)
try:
    while cs.readinto(flush_buf) is not None:
        pass
except OSError:
    pass
```

### 2. Client Stream Wrapper (`TelnetWrapper`)
A custom wrapper implements the standard stream interface (`io.IOBase`), filtering Telnet Interpret As Command (IAC) options and intercepting transport errors or EOF to clean up resources automatically.

```python
class TelnetWrapper(IOBase):
    def __init__(self, socket_obj):
        self.socket = socket_obj
        self.discard_count = 0

    def readinto(self, b):
        try:
            readbytes = 0
            for i in range(len(b)):
                byte = 0
                while byte == 0:
                    data = self.socket.read(1)
                    if data is None:
                        if readbytes == 0:
                            return None
                        return readbytes
                    if len(data) == 0:
                        exit()
                        return 0
                    
                    byte = data[0]
                    if byte == 255:  # IAC (Interpret As Command)
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
```

### 3. Event-Driven Concurrency via Socket Option 20
Instead of spinning a persistent background thread or running a high-frequency polling loop that consumes CPU cycles, the server registers the connection handler (`_cln_handler`) directly on the listening socket and `os.dupterm_notify` on the active client socket. The MicroPython virtual machine executes these callbacks when network interrupts occur.

```python
# Bind and listen on port 23 in non-blocking mode
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('0.0.0.0', 23))
srv.listen(1)
srv.setblocking(False)

# Socket option 20 registers callbacks for asynchronous events
srv.setsockopt(socket.SOL_SOCKET, 20, _cln_handler)
```

---

## Detailed Implementation Lenses

### 1. Memory Management & Footprint
*   **Emergency Exception Buffer:** Configured with `micropython.alloc_emergency_exception_buf(250)` to allow execution of traceback prints inside interrupts and low-memory callbacks.
*   **Aggressive Garbage Collection:** Triggered via `gc.collect()` at strategic points:
    1. After establishing Wi-Fi connection.
    2. After starting the Telnet daemon.
    3. After completing/failing the client handshake authentication.
    4. Upon client disconnection.
*   **Constant Compilation:** Custom port options and system flags are declared using `micropython.const()` to optimize bytecode resolution.

### 2. Concurrency & Timing
*   **Cooperative Stream Integration:** Handled via `os.dupterm()` redirect. The custom stream class performs non-blocking reads and returns `None` when no data is buffered, allowing other scheduled code to run.
*   **Authentication Timeout:** The authentication phase temporarily sets the client socket to blocking with a `15.0` second timeout. If a remote client connects but goes silent, the session times out, freeing the server instead of blocking the main thread indefinitely.

### 3. Resilience & Headless Recovery
*   **Zombie Connection Prevention:** Since standard TCP sockets can remain half-open if a client disconnects unexpectedly, the system implements active stream validation. If a remote client vanishes, the next I/O transaction on the terminal will return an EOF or raise an `OSError`, which immediately closes the defunct stream and restores standard UART console access.
*   **Automatic Handshake Cleanup:** The `TelnetWrapper` monitors the socket for `0` bytes read (EOF) or any network `OSError`. It immediately triggers the `exit()` sequence to restore the console back to UART and ready the daemon for a new connection.
*   **Single-Session Exclusivity:** Only one client is allowed to access the REPL at a time. If a second client attempts to connect while a session is active, the server checks the actual status of the terminal via `os.dupterm(None)`. If the session is alive, it rejects the new client with a busy notice. If it is dead, it garbage-collects the old session and accepts the new one.

---

## Potential Failures & Recovery

| Failure Mode | Impact | Recovery Mechanism |
| :--- | :--- | :--- |
| **Silent Wi-Fi Drop / Reconnection** | Server socket loses interface binding or remote client gets disconnected. | The listening socket is bound to `0.0.0.0`. If Wi-Fi goes down and is reconnected by the system, the socket continues to accept connections without requiring a reboot. |
| **Zombie Client (Unclean Exit)** | The server remains locked, rejecting new incoming Telnet sessions. | **Active Stream Validation:** Any write or read attempt by the REPL or background tasks on the dead socket triggers an EOF or `OSError`, which immediately executes the `exit()` routine, frees the daemon, and registers the REPL back to UART. |
| **Handshake Denial-of-Service** | A client connects and refuses to send login credentials, blocking the socket. | **Authentication Timeout:** A strict 15-second timer is attached to the handshake phase. If exceeded, the socket closes and calls `gc.collect()`. |
| **Power Surge / Reset** | Corruption of operational variables or loop freeze. | MicroPython recovers state via clean boot initialization in `telnetd.py`. Power management is disabled (`pm=0`) during boot to guarantee network card stability. |

---

## System Limitations

1. **Lack of Encryption (Cleartext Protocol):**
   * **Detail:** Telnet transmits all data, including credentials (username and password) and commands, in unencrypted cleartext.
   * **Mitigation:** Only deploy this daemon inside trusted private networks (e.g., local home automation subnets) or behind a VPN. Do not expose port 23 directly to the public internet.

2. **Single Client Limitation:**
   * **Detail:** MicroPython's terminal redirection (`os.dupterm`) supports one concurrent active network stream on a given slot.
   * **Mitigation:** The system rejects additional client connections with a `Server busy` message while the primary terminal session is active.

3. **Telnet Client Negotiation Differences:**
   * **Detail:** Raw TCP clients (like Netcat) do not process the Telnet terminal negotiation sequences (`DONT LINEMODE`, `WILL ECHO`).
   * **Impact:** When connecting with a raw TCP utility, typing might show double characters or lack standard line termination. Connect using standard Telnet utilities (`telnet <IP>`) for optimal performance.

## Final Summary of Changes

1. Namespace Isolation & Security ( telnetd.py ):
    • All imports and configuration scopes are completely encapsulated inside function frames.
    • The  Setup  helper class is cleaned up from the runtime namespace using  del Setup  once initialization completes.
    • Auto-complete and  dir(telnetd)  do not leak sensitive parameters or internal modules.
2. REPL Builtins Integration ( Telnet.py ):
    • Registered  exit()  and  stop()  directly into the MicroPython  builtins  namespace, enabling
      clean execution of these commands directly from the REPL console.
3. Exception Safety & Deactivation Guards ( Telnet.py ):
    • Integrated  self.socket is None  guards inside  TelnetWrapper.readinto()  and  TelnetWrapper. write() to completely prevent  AttributeError  exceptions when the MicroPython event system flushes streams during deactivation.
  4. Warnings Suppression:
      • Removed the unused SO_KEEPALIVE  configuration block to eliminate the platform-level warning  
      Warning: lwip.setsockopt() option not implemented  generated by compiled ESP32 firmware stacks.
  5. Project Documentation:
      • Created the README.md covering design decisions, zero-allocation buffers, event callbacks
      via socket option 20, active stream validation, failure modes, and security limitations.

## usage example

### Server

Edit `config.py`

Change:
- wifi = ("YOUR-SSID", "YOUR-PASSWD") # use by telnetd.py
- appu = ('a', '1234')                # use by telnetd.py

```
>>> import telnetd
Connecting to Wi-Fi: dlink-3530 ...
Wi-Fi connected. IP: 192.168.5.40
Starting Telnet daemon on 192.168.5.40 port 23
Telnet daemon successfully started.
```

### Client

On a Linux PC

```
$ telnet 192.168.5.40
Trying 192.168.5.40...
Connected to 192.168.5.40.
Escape character is '^]'.

*************
** Welcome **
*************

username: a
password: 1234

OK
Press enter to continue. Write exit() to exit session.

>>> dir()
['RemoteFS', 'RemoteCommand', '__file__', 'gc', 'vfs', 'telnetd', '__mount', 'bdev', 'struct', '__name__', 'os', 'io', 'RemoteFile', 'SEEK_SET', 'micropython']
>>> dir(telnetd)
['__class__', '__name__', '__dict__', '__file__']
>>> exit()
Connection closed by foreign host.
```

## Userid and password

The user credential is defined in the `config.py` file.

```
$ grep 1234 *.py
config.py:appu = ('a', '1234')                # use by telnetd.py
```

