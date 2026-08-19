"""
IR Transmitter Module
High-level interface for transmitting IR signals through USB dongles.
"""

import time
from typing import Optional, List, Union
from pathlib import Path
from .device import IRDongle, find_dongle, DeviceInfo
from .protocols import IRSignal, IRProtocol, ProtocolType
from .flipper_parser import FlipperIRParser, load_ir_file


class IRTransmitter:
    """
    High-level IR Transmitter class.
    
    Provides a simple interface for sending IR signals through a USB dongle,
    with support for Flipper Zero .ir files.
    
    Usage:
        transmitter = IRTransmitter()
        transmitter.load_ir_file("remote.ir")
        transmitter.send("POWER")
        transmitter.close()
    """
    
    def __init__(self, dongle: Optional[IRDongle] = None, vid: Optional[int] = None, pid: Optional[int] = None):
        """
        Initialize the IR transmitter.
        
        Args:
            dongle: Optional pre-configured IRDongle instance
            vid: Vendor ID to filter devices
            pid: Product ID to filter devices
        """
        self._dongle = dongle
        self._vid = vid
        self._pid = pid
        self._parser = FlipperIRParser()
        self._signals: List[IRSignal] = []
        self._connected = False
    
    @property
    def connected(self) -> bool:
        """Check if connected to the dongle"""
        return self._connected and self._dongle is not None and self._dongle.is_open
    
    def connect(self) -> bool:
        """
        Connect to the IR dongle.
        
        Returns:
            True if connection successful
        """
        if self._connected:
            return True
        
        if self._dongle is None:
            self._dongle = find_dongle(vid=self._vid, pid=self._pid)
        
        if self._dongle is None:
            print("Error: No IR dongle found")
            return False
        
        if self._dongle.open():
            self._connected = True
            return True
        
        return False
    
    def disconnect(self) -> None:
        """Disconnect from the IR dongle"""
        if self._dongle:
            self._dongle.close()
        self._connected = False
    
    def close(self) -> None:
        """Alias for disconnect()"""
        self.disconnect()
    
    def load_ir_file(self, filepath: str) -> List[str]:
        """
        Load IR signals from a Flipper Zero .ir file.
        
        Args:
            filepath: Path to the .ir file
            
        Returns:
            List of signal names loaded
        """
        self._signals = load_ir_file(filepath)
        return [s.name for s in self._signals]
    
    def get_signal_names(self) -> List[str]:
        """Get list of loaded signal names"""
        return [s.name for s in self._signals]
    
    def get_signal(self, name: str) -> Optional[IRSignal]:
        """
        Get a signal by name.
        
        Args:
            name: Name of the signal
            
        Returns:
            IRSignal object or None if not found
        """
        for signal in self._signals:
            if signal.name == name:
                return signal
        return None
    
    def send(self, name_or_signal: Union[str, IRSignal], repeat: int = 1) -> bool:
        """
        Send an IR signal.
        
        Args:
            name_or_signal: Either a signal name (string) or IRSignal object
            repeat: Number of times to repeat the signal
            
        Returns:
            True if signal was sent successfully
        """
        if not self.connected:
            print("Error: Not connected to dongle")
            return False
        
        # Get the signal
        if isinstance(name_or_signal, str):
            signal = self.get_signal(name_or_signal)
            if signal is None:
                print(f"Error: Signal '{name_or_signal}' not found")
                return False
        else:
            signal = name_or_signal
        
        return self._dongle.send_signal(signal, repeat)
    
    def send_nec(self, address: int, command: int, repeat: int = 1, extended: bool = False) -> bool:
        """
        Send a NEC protocol IR signal.
        
        Args:
            address: Device address (8-bit or 16-bit if extended)
            command: Command byte
            repeat: Number of repeats
            extended: Use extended NEC format (16-bit address)
            
        Returns:
            True if sent successfully
        """
        signal = IRProtocol.encode_signal(ProtocolType.NEC, address, command, extended=extended)
        return self.send(signal, repeat)
    
    def send_samsung(self, address: int, command: int, repeat: int = 1) -> bool:
        """Send a Samsung32 protocol IR signal"""
        signal = IRProtocol.encode_signal(ProtocolType.SAMSUNG32, address, command)
        return self.send(signal, repeat)
    
    def send_rc5(self, address: int, command: int, repeat: int = 1, toggle: bool = False) -> bool:
        """Send an RC5 protocol IR signal"""
        signal = IRProtocol.encode_signal(ProtocolType.RC5, address, command, toggle=toggle)
        return self.send(signal, repeat)
    
    def send_rc6(self, address: int, command: int, repeat: int = 1, toggle: bool = False) -> bool:
        """Send an RC6 protocol IR signal"""
        signal = IRProtocol.encode_signal(ProtocolType.RC6, address, command, toggle=toggle)
        return self.send(signal, repeat)
    
    def send_sirc(self, address: int, command: int, repeat: int = 1, bits: int = 12) -> bool:
        """Send a Sony SIRC protocol IR signal"""
        if bits == 15:
            protocol = ProtocolType.SIRC15
        elif bits == 20:
            protocol = ProtocolType.SIRC20
        else:
            protocol = ProtocolType.SIRC
        signal = IRProtocol.encode_signal(protocol, address, command)
        return self.send(signal, repeat)
    
    def send_raw(self, timings: List[int], frequency: int = 38000, duty_cycle: float = 0.33, repeat: int = 1) -> bool:
        """
        Send raw IR timings.
        
        Args:
            timings: List of mark/space timings in microseconds
            frequency: Carrier frequency in Hz
            duty_cycle: Duty cycle (0.0-1.0)
            repeat: Number of repeats
            
        Returns:
            True if sent successfully
        """
        signal = IRSignal(
            name="raw",
            protocol=ProtocolType.RAW,
            frequency=frequency,
            duty_cycle=duty_cycle,
            timings=timings
        )
        return self.send(signal, repeat)
    
    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def quick_send(signal_name: str, ir_file: str, 
               vid: Optional[int] = None, pid: Optional[int] = None) -> bool:
    """
    Quick helper function to send a single signal from an IR file.
    
    Args:
        signal_name: Name of the signal to send
        ir_file: Path to .ir file
        vid: Optional vendor ID filter
        pid: Optional product ID filter
        
    Returns:
        True if successful
    """
    with IRTransmitter(vid=vid, pid=pid) as tx:
        tx.load_ir_file(ir_file)
        return tx.send(signal_name)


def list_ir_files(directory: str = ".") -> List[Path]:
    """
    List all .ir files in a directory.
    
    Args:
        directory: Directory to search
        
    Returns:
        List of Path objects for .ir files
    """
    path = Path(directory)
    return list(path.glob("**/*.ir"))