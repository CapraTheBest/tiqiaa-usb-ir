"""
Flipper Zero .ir File Parser
Parses Flipper Zero IR signal files (.ir format) into usable IR signals.
"""

import re
from pathlib import Path
from typing import List, Optional, Dict, Any
from .protocols import IRSignal, ProtocolType, IRProtocol


class FlipperIRParser:
    """Parser for Flipper Zero .ir file format"""
    
    # Flipper Zero IR file versions supported
    SUPPORTED_VERSIONS = [1]
    
    def __init__(self):
        self.signals: List[IRSignal] = []
        self._current_signal: Dict[str, Any] = {}
    
    def parse_file(self, filepath: str) -> List[IRSignal]:
        """
        Parse a Flipper Zero .ir file and return a list of IR signals.
        
        Args:
            filepath: Path to the .ir file
            
        Returns:
            List of IRSignal objects
        """
        self.signals = []
        path = Path(filepath)
        
        if not path.exists():
            raise FileNotFoundError(f"IR file not found: {filepath}")
        
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return self._parse_content(content)
    
    def _parse_content(self, content: str) -> List[IRSignal]:
        """Parse the content of an IR file"""
        lines = content.strip().split('\n')
        
        # Parse header
        self._parse_header(lines)
        
        # Parse signals (separated by '#')
        signal_blocks = []
        current_block = []
        
        for line in lines:
            stripped = line.strip()
            if stripped == '#':
                if current_block:
                    signal_blocks.append(current_block)
                    current_block = []
            else:
                current_block.append(stripped)
        
        # Don't forget the last block
        if current_block:
            signal_blocks.append(current_block)
        
        # Parse each signal block
        for block in signal_blocks:
            signal = self._parse_signal_block(block)
            if signal:
                self.signals.append(signal)
        
        return self.signals
    
    def _parse_header(self, lines: List[str]) -> None:
        """Parse and validate file header"""
        filetype = None
        version = None
        
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('Filetype:'):
                filetype = stripped.split(':', 1)[1].strip()
            elif stripped.startswith('Version:'):
                try:
                    version = int(stripped.split(':', 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith('#'):
                break  # End of header
        
        if filetype and 'IR' not in filetype:
            raise ValueError(f"Invalid file type: {filetype}. Expected IR signals file.")
        
        if version and version not in self.SUPPORTED_VERSIONS:
            print(f"Warning: File version {version} may not be fully supported.")
    
    def _parse_signal_block(self, block: List[str]) -> Optional[IRSignal]:
        """Parse a single signal block"""
        signal_data = {}
        
        for line in block:
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip().lower()
                value = value.strip()
                signal_data[key] = value
        
        # Skip file header blocks (contain filetype/version but no name)
        if 'filetype' in signal_data or 'version' in signal_data:
            if 'name' not in signal_data:
                return None
        
        # Skip empty blocks or blocks without required fields
        if 'name' not in signal_data:
            return None
        
        # Check signal type
        signal_type = signal_data.get('type', 'raw')
        
        if signal_type == 'parsed':
            return self._parse_parsed_signal(signal_data)
        elif signal_type == 'raw':
            return self._parse_raw_signal(signal_data)
        else:
            print(f"Warning: Unknown signal type '{signal_type}'")
            return None
    
    def _parse_parsed_signal(self, data: Dict[str, str]) -> Optional[IRSignal]:
        """Parse a 'parsed' type signal (protocol-based)"""
        name = data.get('name', 'Unknown')
        protocol_str = data.get('protocol', '')
        address_str = data.get('address', '00 00 00 00')
        command_str = data.get('command', '00 00 00 00')
        
        # Parse protocol
        protocol = IRProtocol.parse_protocol_type(protocol_str)
        
        # Parse address and command (Flipper uses space-separated hex bytes, little-endian)
        address = self._parse_flipper_hex(address_str)
        command = self._parse_flipper_hex(command_str)
        
        # Generate timings from protocol
        try:
            # Determine if this is an extended NEC
            extended = False
            if protocol == ProtocolType.NEC and address > 0xFF:
                extended = True
            
            signal = IRProtocol.encode_signal(protocol, address, command, extended=extended)
            signal.name = name
            return signal
        except ValueError as e:
            print(f"Warning: Could not encode signal '{name}': {e}")
            # Return raw fallback if encoding fails
            return IRSignal(
                name=name,
                protocol=protocol,
                frequency=38000,
                duty_cycle=0.33,
                timings=[],
                address=address,
                command=command
            )
    
    def _parse_raw_signal(self, data: Dict[str, str]) -> Optional[IRSignal]:
        """Parse a 'raw' type signal (timing-based)"""
        name = data.get('name', 'Unknown')
        
        try:
            frequency = int(data.get('frequency', '38000'))
        except ValueError:
            frequency = 38000
        
        try:
            duty_cycle = float(data.get('duty_cycle', '0.33'))
        except ValueError:
            duty_cycle = 0.33
        
        # Parse timing data
        timings_data = data.get('data', '')
        timings = self._parse_timings(timings_data)
        
        return IRSignal(
            name=name,
            protocol=ProtocolType.RAW,
            frequency=frequency,
            duty_cycle=duty_cycle,
            timings=timings,
            address=None,
            command=None
        )
    
    def _parse_flipper_hex(self, hex_str: str) -> int:
        """
        Parse Flipper Zero hex format (space-separated bytes, little-endian).
        Example: "07 00 00 00" -> 0x00000007 -> 7
        """
        parts = hex_str.split()
        if not parts:
            return 0
        
        # Convert bytes (little-endian)
        result = 0
        for i, part in enumerate(parts):
            try:
                byte_val = int(part, 16)
                result |= byte_val << (i * 8)
            except ValueError:
                pass
        
        return result
    
    def _parse_timings(self, timings_str: str) -> List[int]:
        """
        Parse timing data from Flipper Zero format.
        Can be space-separated or a single line of numbers.
        """
        timings = []
        
        # Handle both space-separated and multi-line formats
        parts = timings_str.split()
        
        for part in parts:
            try:
                # Skip empty strings
                if part:
                    value = int(part)
                    timings.append(value)
            except ValueError:
                # Skip non-numeric values
                pass
        
        return timings
    
    def get_signal_by_name(self, name: str) -> Optional[IRSignal]:
        """Get a signal by its name"""
        for signal in self.signals:
            if signal.name == name:
                return signal
        return None
    
    def list_signals(self) -> List[str]:
        """List all signal names in the file"""
        return [signal.name for signal in self.signals]


def load_ir_file(filepath: str) -> List[IRSignal]:
    """
    Convenience function to load and parse a Flipper Zero .ir file.
    
    Args:
        filepath: Path to the .ir file
        
    Returns:
        List of IRSignal objects
    """
    parser = FlipperIRParser()
    return parser.parse_file(filepath)