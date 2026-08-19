"""
USB IR Dongle Device Communication Module
Handles detection and communication with USB-C IR dongles on Windows.
Supports multiple communication methods: HID, Serial (CDC-ACM), and raw USB.
"""

import time
import struct
from typing import Optional, List, Tuple, Dict, Any
from dataclasses import dataclass
from enum import Enum
from .protocols import IRSignal


def _get_libusb_backend():
    """Return a pyusb libusb1 backend, using the bundled libusb DLL on Windows.

    Without this pyusb raises NoBackendError on Windows even when the `libusb`
    package is installed, because it can't auto-locate the DLL.
    """
    try:
        import usb.backend.libusb1
        import libusb
        import os
        dll = os.path.join(
            os.path.dirname(libusb.__file__),
            "_platform", "windows", "x86_64", "libusb-1.0.dll",
        )
        if os.path.exists(dll):
            return usb.backend.libusb1.get_backend(find_library=lambda x: dll)
    except Exception:
        pass
    return None  # fall back to pyusb's default discovery


# Carrier frequency -> Tiqiaa index. Index 0 is the common 38 kHz path (native applies
# the x16 / unit=16us scaling). Captured TV codes used index 5; the full Hz->index table
# Default to the 38 kHz path for standard codes.
# Carrier-frequency index table (index -> Hz), as the dongle firmware orders them. The frame's
# freq_index byte selects which carrier the dongle modulates at. Getting this right matters:
# e.g. Sony needs 40000Hz (index 4), not 38000 (index 0) -- wrong carrier => TV ignores the IR.
_TIQIAA_FREQ_TABLE = [
    38000, 37900, 37917, 36000, 40000, 39700, 35750, 36400, 36700, 37000,
    37700, 38380, 38400, 38462, 38740, 39200, 42000, 43600, 44000, 33000,
    33500, 34000, 34500, 35000, 40500, 41000, 41500, 42500, 43000, 45000,
]


def _freq_to_index(frequency_hz: int) -> int:
    """Map a carrier frequency (Hz) to the nearest Tiqiaa freq-table index.
    Nearest match, as the firmware does. freq 0/unknown -> 0 (38kHz, the common default)."""
    if not frequency_hz or frequency_hz < 1000:
        return 0
    best_i, best_d = 0, None
    for i, f in enumerate(_TIQIAA_FREQ_TABLE):
        d = abs(f - frequency_hz)
        if best_d is None or d < best_d:
            best_i, best_d = i, d
            if d == 0:
                break
    return best_i


class _TiqiaaInReader:
    """Continuous background reader for the IN endpoint.

    The dongle must have its IN endpoint drained continuously or it stops emitting IR, so
    this thread is not just housekeeping: it is half of the request/response flow control
    that keeps the device alive. See wait_reply() and TiqiaaProtocol.transmit_rle.
    """

    def __init__(self, dev, ep_in: int):
        import threading
        self._dev = dev
        self._ep_in = ep_in
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Replies land here and waiters are woken: the reader thread pushes each reply onto
        # the list and notifies whoever the send path has blocked waiting for it.
        self._cond = threading.Condition()
        self._replies = []
        self.stall_count = 0

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1)

    def drain_replies(self):
        """Pop and return the replies seen since the last call (diagnostics)."""
        with self._cond:
            out, self._replies = self._replies, []
        return out

    def wait_reply(self, timeout_ms: int):
        """Block until the dongle answers, or `timeout_ms` elapses. Returns the reply
        (bytes) or None on timeout.

        This is the flow control that keeps the device alive: it only answers an IR data
        frame once the IR has actually been emitted, so waiting for that reply is what
        stops us pushing the next payload into a mid-transmission firmware.
        """
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        with self._cond:
            while not self._replies:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._replies.pop(0)

    def clear_replies(self):
        """Drop any pending replies. Done immediately before sending, so a stale reply can
        never be mistaken for the new command's acknowledgement."""
        with self._cond:
            self._replies.clear()

    def _run(self):
        import time
        import usb.core
        while not self._stop.is_set():
            try:
                data = self._dev.read(self._ep_in, 64, timeout=50)
                if len(data):
                    with self._cond:
                        self._replies.append(bytes(data))
                        if len(self._replies) > 256:
                            del self._replies[:-256]
                        self._cond.notify_all()
            except usb.core.USBError as e:
                # A read timeout is the normal idle case and must NOT be counted. Anything
                # else (notably a pipe error / STALL) is counted so a caller can see the pipe
                # went bad -- a halted IN pipe stops the dongle emitting IR entirely.
                msg = str(e).lower()
                timed_out = (getattr(e, "errno", None) == 110
                             or "timed out" in msg or "timeout" in msg)
                if not timed_out:
                    self.stall_count += 1
                time.sleep(0.005)  # timeout / no data -> brief pause, keep polling


class _TiqiaaUsbWriter:
    """Adapts a pyusb device to TiqiaaProtocol's .bulk_transfer(bytes) API.

    Only writes to the OUT endpoint -- the IN endpoint is drained by a concurrent
    _TiqiaaInReader thread (the device requires the IN pipe to stay armed).
    """

    def __init__(self, dev, ep_out: int, ep_in: int, reader=None):
        self._dev = dev
        self._ep_out = ep_out
        self._ep_in = ep_in
        self._reader = reader
        if reader is None:
            # Without a background reader nothing is draining IN, so we cannot wait for
            # replies. Hide the capability (rather than returning None, which the protocol
            # would read as "timed out") so it falls back to fire-and-forget.
            self.wait_reply = None
            self.clear_replies = None

    def wait_reply(self, timeout_ms: int):
        """Block for the dongle's reply via the background reader (see its wait_reply)."""
        return self._reader.wait_reply(timeout_ms)

    def clear_replies(self):
        if self._reader is not None:
            self._reader.clear_replies()

    def bulk_transfer(self, data: bytes) -> int:
        import usb.core
        try:
            return self._dev.write(self._ep_out, data, timeout=1000)
        except usb.core.USBError:
            # Pipe/IO error: clear the OUT halt and retry once.
            try:
                self._dev.clear_halt(self._ep_out)
            except usb.core.USBError:
                pass
            return self._dev.write(self._ep_out, data, timeout=1000)

    def bulk_read(self, n: int = 64, timeout: int = 60):
        """Read the IN endpoint reply, draining the device's poll/header responses.
        Keeps the dongle armed, which is what makes multi-send reliable."""
        import usb.core
        try:
            return self._dev.read(self._ep_in, n, timeout=timeout)
        except usb.core.USBError:
            return b""


class DeviceType(Enum):
    """Type of USB device communication"""
    HID = "hid"
    SERIAL = "serial"
    RAW_USB = "raw_usb"
    UNKNOWN = "unknown"


@dataclass
class DeviceInfo:
    """Information about a detected IR dongle"""
    vid: int  # Vendor ID
    pid: int  # Product ID
    device_type: DeviceType
    path: str  # Device path or COM port
    description: str
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    serial_number: Optional[str] = None
    
    @property
    def vid_pid_str(self) -> str:
        return f"{self.vid:04X}:{self.pid:04X}"


class IRDongle:
    """
    USB-C IR Dongle interface.
    
    This class provides methods to communicate with IR dongles using
    various protocols (HID, Serial, or raw USB).
    
    Usage:
        dongle = find_dongle()
        if dongle:
            dongle.open()
            dongle.send_signal(ir_signal)
            dongle.close()
    """
    
    def __init__(self, device_info: DeviceInfo):
        self.device_info = device_info
        self._device = None
        self._is_open = False
        self._hid_device = None
        self._serial_device = None
        # ONE persistent protocol per dongle. The counters it uses are process-wide
        # (see _WireCounters) because the dongle keeps its sequence state across a handle
        # close -- restarting them per connection replays sequences it has already retired.
        self._proto = None
        # Background IN-endpoint reader (required for multi-send; see _TiqiaaInReader).
        self._reader = None
    
    @property
    def is_open(self) -> bool:
        return self._is_open
    
    def open(self) -> bool:
        """
        Open connection to the IR dongle.
        
        Returns:
            True if connection established successfully
        """
        if self._is_open:
            return True
        
        try:
            if self.device_info.device_type == DeviceType.HID:
                return self._open_hid()
            elif self.device_info.device_type == DeviceType.SERIAL:
                return self._open_serial()
            elif self.device_info.device_type == DeviceType.RAW_USB:
                return self._open_raw_usb()
            else:
                # Try each method
                if self._open_hid():
                    self.device_info.device_type = DeviceType.HID
                    return True
                if self._open_serial():
                    self.device_info.device_type = DeviceType.SERIAL
                    return True
                if self._open_raw_usb():
                    self.device_info.device_type = DeviceType.RAW_USB
                    return True
                
                print("Error: Could not open device with any supported method")
                return False
        except Exception as e:
            print(f"Error opening device: {e}")
            return False
    
    def _open_hid(self) -> bool:
        """Open device using HID protocol"""
        try:
            import hid
            self._hid_device = hid.device()
            self._hid_device.open(self.device_info.vid, self.device_info.pid)
            self._device = self._hid_device
            self._is_open = True
            
            # Set non-blocking mode
            self._hid_device.set_nonblocking(True)
            
            print(f"HID device opened: {self.device_info.vid_pid_str}")
            return True
        except ImportError:
            print("hidapi not installed. Install with: pip install hidapi")
            return False
        except Exception as e:
            print(f"Failed to open HID device: {e}")
            self._hid_device = None
            return False
    
    def _open_serial(self) -> bool:
        """Open device using Serial (CDC-ACM) protocol"""
        try:
            import serial
            self._serial_device = serial.Serial(
                port=self.device_info.path,
                baudrate=115200,  # Common default for IR dongles
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1
            )
            self._device = self._serial_device
            self._is_open = True
            
            print(f"Serial device opened: {self.device_info.path}")
            return True
        except ImportError:
            print("pyserial not installed. Install with: pip install pyserial")
            return False
        except Exception as e:
            print(f"Failed to open serial device: {e}")
            self._serial_device = None
            return False
    
    def _open_raw_usb(self) -> bool:
        """Open device using raw USB (pyusb) with the libusb backend.

        For the Tiqiaa/JOYROOM dongle this is the path that actually works: the
        device is driven via bulk transfers on endpoint 0x01 using TiqiaaProtocol
        (see docs/PROTOCOL.md).
        """
        try:
            import usb.core
            import usb.util

            backend = _get_libusb_backend()
            self._device = usb.core.find(
                idVendor=self.device_info.vid,
                idProduct=self.device_info.pid,
                backend=backend,
            )

            if self._device is None:
                print("Device not found via pyusb")
                return False

            # NOTE: no reset() here. The device does not inherently wedge after a send --
            # many commands can share one handle. The real cause was sending the
            # next command while the dongle was still transmitting the previous one; fixed
            # by waiting for its acknowledgement (see TiqiaaProtocol.transmit_rle).
            # reset() is useless here anyway: once the firmware is wedged, clear_halt,
            # set_configuration and reset() all fail to recover it -- only a replug does.
            try:
                if self._device.is_kernel_driver_active(0):
                    self._device.detach_kernel_driver(0)
            except (NotImplementedError, usb.core.USBError):
                pass  # Windows doesn't support this

            self._device.set_configuration()
            # Do NOT explicitly claim/release the interface: WinUSB owns it and the
            # claim/release dance (combined with reset) was an extra wedge source.
            # Start the continuous IN-endpoint reader -- REQUIRED both for the dongle to
            # emit IR at all and for the flow control (it is what receives the "transmitted"
            # acknowledgement that transmit_rle blocks on).
            self._reader = _TiqiaaInReader(self._device, self.TIQIAA_EP_IN)
            self._reader.start()
            self._is_open = True
            print(f"Raw USB device opened: {self.device_info.vid_pid_str}")
            return True
        except ImportError:
            print("pyusb not installed. Install with: pip install pyusb")
            return False
        except Exception as e:
            print(f"Failed to open raw USB device: {e}")
            return False
    
    def close(self) -> None:
        """Close connection to the IR dongle"""
        if not self._is_open:
            return
        
        try:
            if self._hid_device:
                self._hid_device.close()
                self._hid_device = None
            elif self._serial_device:
                self._serial_device.close()
                self._serial_device = None
            elif self._device:
                import usb.util
                # Stop the background IN reader before disposing the handle.
                if self._reader is not None:
                    self._reader.stop()
                    self._reader = None
                # Just dispose handles; do NOT release_interface/reset here -- on
                # Windows/WinUSB that wedges the CP210x dongle until a physical replug.
                usb.util.dispose_resources(self._device)
                self._device = None
        except Exception as e:
            print(f"Error closing device: {e}")
        finally:
            self._is_open = False
    
    def send_signal(self, signal: IRSignal, repeat: int = 1) -> bool:
        """
        Send an IR signal through the dongle.
        
        Args:
            signal: IRSignal object containing the IR data
            repeat: Number of times to repeat the signal (for button presses)
            
        Returns:
            True if signal was sent successfully
        """
        if not self._is_open:
            print("Error: Device not open")
            return False
        
        if not signal.timings:
            print(f"Warning: Signal '{signal.name}' has no timings")
            return False
        
        try:
            if self.device_info.device_type == DeviceType.HID:
                return self._send_hid(signal, repeat)
            elif self.device_info.device_type == DeviceType.SERIAL:
                return self._send_serial(signal, repeat)
            elif self.device_info.device_type == DeviceType.RAW_USB:
                return self._send_raw_usb(signal, repeat)
            else:
                print("Error: Unknown device type")
                return False
        except Exception as e:
            print(f"Error sending signal: {e}")
            return False
    
    def _send_hid(self, signal: IRSignal, repeat: int) -> bool:
        """Send IR signal via HID"""
        # Build HID report with IR data
        # Note: The exact format depends on the dongle's firmware
        # This is a generic implementation that may need adjustment
        
        for _ in range(repeat):
            # Common HID IR dongle format:
            # Byte 0: Report ID (usually 0)
            # Bytes 1-2: Frequency (little-endian)
            # Byte 3: Duty cycle (0-100)
            # Bytes 4-5: Timing count
            # Bytes 6+: Timing data (pairs of mark/space in microseconds)
            
            report = bytearray()
            report.append(0x00)  # Report ID
            
            # Frequency (16-bit, little-endian)
            freq = signal.frequency
            report.append(freq & 0xFF)
            report.append((freq >> 8) & 0xFF)
            
            # Duty cycle (percentage)
            report.append(int(signal.duty_cycle * 100))
            
            # Timing count (16-bit)
            timing_count = min(len(signal.timings), 255)  # Limit to 255 for safety
            report.append(timing_count & 0xFF)
            report.append((timing_count >> 8) & 0xFF)
            
            # Timing data (each timing as 16-bit value)
            for timing in signal.timings[:timing_count]:
                report.append(timing & 0xFF)
                report.append((timing >> 8) & 0xFF)
            
            # Send in chunks if report is too long (HID typically 64 bytes max)
            chunk_size = 62  # Leave room for chunk header
            chunks = [report[i:i + chunk_size] for i in range(0, len(report), chunk_size)]
            
            for idx, chunk in enumerate(chunks):
                # Add chunk info for multi-chunk reports
                hid_report = bytearray([idx, len(chunks)]) + chunk
                
                # Pad to standard HID report size
                while len(hid_report) < 64:
                    hid_report.append(0)
                
                self._hid_device.write(hid_report)
                time.sleep(0.01)  # Small delay between chunks
            
            # Wait between repeats (typical IR repeat is 100-200ms)
            if repeat > 1:
                time.sleep(0.1)
        
        return True
    
    def _send_serial(self, signal: IRSignal, repeat: int) -> bool:
        """Send IR signal via Serial"""
        # Serial IR dongles often use a text-based or simple binary protocol
        # This is a generic implementation
        
        for _ in range(repeat):
            # Format: <freq>|<duty>|<timings...>
            # Example: 38000|33|9000,4500,560,560,560,1690,...
            
            timings_str = ','.join(str(t) for t in signal.timings)
            command = f"{signal.frequency}|{int(signal.duty_cycle * 100)}|{timings_str}\n"
            
            self._serial_device.write(command.encode('utf-8'))
            self._serial_device.flush()
            
            # Wait for response (optional)
            time.sleep(0.05)
            
            # Wait between repeats
            if repeat > 1:
                time.sleep(0.1)
        
        return True
    
    # Tiqiaa dongle USB endpoints (confirmed from capture + native lib)
    TIQIAA_EP_OUT = 0x01
    TIQIAA_EP_IN = 0x81

    def _get_proto(self):
        """Return the persistent TiqiaaProtocol for this dongle (shared cmd_id)."""
        if self._proto is None:
            from .tiqiaa_protocol import TiqiaaProtocol
            self._proto = TiqiaaProtocol()
        return self._proto

    def _send_raw_usb(self, signal: IRSignal, repeat: int) -> bool:
        """Send IR signal via the Tiqiaa USB protocol on endpoint 0x01.

        Uses TiqiaaProtocol (see docs/PROTOCOL.md): the carrier
        is encoded as a frequency index, marks have MSB set, and frames are written
        as bulk transfers with the device's IN endpoint drained between writes.
        """
        proto = self._get_proto()
        adapter = _TiqiaaUsbWriter(self._device, self.TIQIAA_EP_OUT, self.TIQIAA_EP_IN,
                                   reader=self._reader)

        for _ in range(repeat):
            ok = proto.transmit(
                adapter,
                signal.frequency,
                signal.timings,
                freq_index=_freq_to_index(signal.frequency),
            )
            if not ok:
                return False
            if repeat > 1:
                time.sleep(0.1)
        return True
    
    def send_ir_rle(self, rle: bytes, freq_index: int = 5, repeat: int = 1,
                    flags=None) -> bool:
        """Send a pre-encoded Tiqiaa RLE IR payload verbatim (e.g. a captured code).

        Use this for a captured payload, whose exact bytes a timing round-trip may not
        reproduce.

        `flags` is accepted and ignored -- that wire byte is the per-command sequence
        counter, now managed by TiqiaaProtocol (see its module docstring).
        """
        if not self._is_open or self.device_info.device_type != DeviceType.RAW_USB:
            print("Error: send_ir_rle requires an open RAW_USB Tiqiaa device")
            return False
        proto = self._get_proto()
        adapter = _TiqiaaUsbWriter(self._device, self.TIQIAA_EP_OUT, self.TIQIAA_EP_IN,
                                   reader=self._reader)
        for _ in range(repeat):
            if not proto.transmit_rle(adapter, rle, freq_index=freq_index):
                return False
            if repeat > 1:
                time.sleep(0.1)
        return True

    def send_key(self, key, repeats: int = 10) -> bool:
        """Send one IR key the way a real remote does.

        `key` is anything with `.timings` (list[int] microseconds) and `.freq` (Hz), e.g.
        any object with those attributes. The carrier index is derived from the frequency.

        WHY repeats=10: a real remote transmits a button press as the IR frame REPEATED
        ~10 times within ONE transmission. Many receivers -- Sony SIRC especially -- need
        several repeats to register, and ignore a single frame. So the timings are repeated
        `repeats` times (joined by their natural trailing inter-frame gap) and sent as ONE
        RLE payload. Note this is one transmission, not N separate sends.
        """
        from .tiqiaa_encode import encode_timings
        timings = getattr(key, "timings", None)
        if not timings:
            return False
        framed = list(timings) * max(1, repeats)   # N frames in ONE transmission
        rle = encode_timings(framed)
        freq_index = _freq_to_index(getattr(key, "freq", 0) or 0)
        return self.send_ir_rle(rle, freq_index=freq_index, repeat=1)

    def send_raw(self, data: bytes) -> bool:
        """
        Send raw data to the device.
        Useful for testing and debugging unknown protocols.
        """
        if not self._is_open:
            print("Error: Device not open")
            return False
        
        try:
            if self._hid_device:
                return self._hid_device.write(data) >= 0
            elif self._serial_device:
                return self._serial_device.write(data) > 0
            elif self._device:
                import usb.util
                cfg = self._device.get_active_configuration()
                intf = cfg[(0, 0)]
                ep_out = usb.util.find_descriptor(
                    intf,
                    custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                )
                if ep_out:
                    ep_out.write(data)
                    return True
            return False
        except Exception as e:
            print(f"Error sending raw data: {e}")
            return False
    
    def read(self, size: int = 64, timeout: int = 1000) -> Optional[bytes]:
        """
        Read data from the device.
        Useful for checking status or responses.
        """
        if not self._is_open:
            return None
        
        try:
            if self._hid_device:
                return bytes(self._hid_device.read(size, timeout))
            elif self._serial_device:
                return self._serial_device.read(size)
            elif self._device:
                import usb.util
                cfg = self._device.get_active_configuration()
                intf = cfg[(0, 0)]
                ep_in = usb.util.find_descriptor(
                    intf,
                    custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                )
                if ep_in:
                    return bytes(ep_in.read(size, timeout))
        except Exception as e:
            pass  # Timeout or no data
        
        return None
    
    def __enter__(self):
        self.open()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def find_dongle(vid: Optional[int] = None, pid: Optional[int] = None) -> Optional[IRDongle]:
    """
    Find and return an IR dongle device.
    
    Args:
        vid: Optional specific Vendor ID to look for
        pid: Optional specific Product ID to look for
        
    Returns:
        IRDongle object if found, None otherwise
    """
    devices = enumerate_devices()
    
    if not devices:
        print("No IR dongle devices found")
        return None
    
    # Filter by VID/PID if specified
    if vid is not None:
        devices = [d for d in devices if d.vid == vid]
    if pid is not None:
        devices = [d for d in devices if d.pid == pid]
    
    if not devices:
        print(f"No device found matching VID={vid:04X}, PID={pid:04X}" if vid and pid else "No matching device found")
        return None
    
    # Return first matching device
    device = devices[0]
    print(f"Found IR dongle: {device.description} ({device.vid_pid_str})")
    return IRDongle(device)


# Tiqiaa / JOYROOM dongle identifiers (Silicon Labs CP210x based)
TIQIAA_VIDS = (0x10C4, 0x045E)
TIQIAA_PID = 0x8468


def find_tiqiaa_dongle() -> Optional["IRDongle"]:
    """Return the Tiqiaa/JOYROOM dongle as a RAW_USB IRDongle (the working path).

    Unlike find_dongle(), this forces DeviceType.RAW_USB and the correct VID/PID so
    the TiqiaaProtocol bulk-transfer path on endpoint 0x01 is used. Call .open() then
    .send_ir_rle(...) or .send_signal(...).
    """
    for vid in TIQIAA_VIDS:
        info = DeviceInfo(
            vid=vid, pid=TIQIAA_PID, device_type=DeviceType.RAW_USB,
            path=f"{vid:04X}:{TIQIAA_PID:04X}",
            description="TiQiaa Tview / JOYROOM JR-CY278",
        )
        dongle = IRDongle(info)
        if dongle.open():
            return dongle
    print("Tiqiaa dongle not found (is it plugged in with the WinUSB driver?)")
    return None


_SHARED_DONGLE = None


def get_shared_dongle():
    """Return a process-wide open dongle, opening it on first use.

    One connection is held for the whole session. See send_key_reliable
    for why this replaced the old open/close-per-press approach.
    """
    global _SHARED_DONGLE
    if _SHARED_DONGLE is not None and _SHARED_DONGLE.is_open:
        return _SHARED_DONGLE
    _SHARED_DONGLE = find_tiqiaa_dongle()
    return _SHARED_DONGLE


def close_shared_dongle() -> None:
    """Close the shared connection (call on exit; not required for correctness)."""
    global _SHARED_DONGLE
    if _SHARED_DONGLE is not None:
        _SHARED_DONGLE.close()
        _SHARED_DONGLE = None


def send_key_reliable(key, repeats: int = 10) -> bool:
    """Fire one IR key, waiting for the dongle to confirm it went out.

    HISTORY -- read this before "optimising" it back:
    This used to open and close the USB handle for EVERY press, on the theory that the dongle
    wedges after a few commands on one connection and that reopening resets it. Both halves
    of that theory were wrong (corrected 2026-08-19, verified on hardware over 100+ commands
    on a single connection):
      * The wedge was never about the connection. We were firing the next command on a fixed
        sleep while the dongle was still transmitting the previous one; that corrupts the
        firmware unrecoverably. TiqiaaProtocol now waits for the device's acknowledgement,
        and the wedge is gone.
      * Reopening actively made it WORSE: a new connection built a new TiqiaaProtocol, which
        restarted the sequence counter, so every press went out as a byte-identical duplicate
        of the last one. Closing the handle does not reset the dongle -- only a replug does.

    So this now reuses one shared connection, which is both correct and much faster per press.

    `repeats` is passed through to send_key (~10 in-frame repeats per press is typical).
    `key` is anything with .timings (list[int] us) and .freq (Hz).
    Returns True once the dongle confirms the IR was transmitted.
    """
    dongle = get_shared_dongle()
    if not dongle:
        return False
    return dongle.send_key(key, repeats=repeats)


# Known IR dongle VID/PID pairs
KNOWN_IR_DONGLES = [
    (0x10C4, 0x8468, "TiQiaa Tview / JOYROOM JR-CY278 / JR-IR02"),
]

def enumerate_devices() -> List[DeviceInfo]:
    """
    Enumerate all potential IR dongle devices.
    
    Returns:
        List of DeviceInfo objects for detected devices
    """
    devices = []
    known_vpid = {(vid, pid) for vid, pid, _ in KNOWN_IR_DONGLES}
    
    # Try HID devices
    try:
        import hid
        for device in hid.enumerate():
            # Look for devices that might be IR dongles
            # Common keywords in product strings
            product = device.get('product_string', '') or ''
            usage_page = device.get('usage_page', 0)
            
            # HID usage page 0x0C is Consumer (common for remotes)
            # Also check for keywords
            ir_keywords = ['ir', 'infrared', 'remote', 'control', 'otg']
            is_potential_ir = (
                any(kw in product.lower() for kw in ir_keywords) or
                usage_page == 0x0C  # Consumer devices
            )
            
            if is_potential_ir or product:  # Include all named HID devices for now
                devices.append(DeviceInfo(
                    vid=device['vendor_id'],
                    pid=device['product_id'],
                    device_type=DeviceType.HID,
                    path=device['path'].decode() if isinstance(device['path'], bytes) else device['path'],
                    description=product or f"HID Device {device['vendor_id']:04X}:{device['product_id']:04X}",
                    manufacturer=device.get('manufacturer_string'),
                    product=product,
                    serial_number=device.get('serial_number')
                ))
    except ImportError:
        pass
    
    # Try serial ports
    try:
        import serial.tools.list_ports
        for port in serial.tools.list_ports.comports():
            # Check for USB serial devices
            if port.vid is not None:
                devices.append(DeviceInfo(
                    vid=port.vid,
                    pid=port.pid or 0,
                    device_type=DeviceType.SERIAL,
                    path=port.device,
                    description=port.description or f"Serial Port {port.device}",
                    manufacturer=port.manufacturer,
                    product=port.description,
                    serial_number=port.serial_number
                ))
    except ImportError:
        pass
    
    # Try raw USB (pyusb)
    try:
        import usb.core
        for device in usb.core.find(find_all=True):
            try:
                product = usb.util.get_string(device, device.iProduct) or ''
                manufacturer = usb.util.get_string(device, device.iManufacturer) or ''
                
                devices.append(DeviceInfo(
                    vid=device.idVendor,
                    pid=device.idProduct,
                    device_type=DeviceType.RAW_USB,
                    path=f"{device.idVendor:04X}:{device.idProduct:04X}",
                    description=product or f"USB Device {device.idVendor:04X}:{device.idProduct:04X}",
                    manufacturer=manufacturer,
                    product=product
                ))
            except Exception:
                continue
    except ImportError:
        pass
    
    return devices


def list_available_devices() -> List[DeviceInfo]:
    """List all available USB devices that could be IR dongles"""
    devices = enumerate_devices()
    print("Available USB devices:")
    for i, device in enumerate(devices):
        print(f"  {i}: {device.description}")
        print(f"      VID:PID = {device.vid_pid_str}")
        print(f"      Type = {device.device_type.value}")
        print(f"      Path = {device.path}")
        if device.manufacturer:
            print(f"      Manufacturer = {device.manufacturer}")
        print()
    return devices