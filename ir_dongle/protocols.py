"""
IR Protocol Encoders
Supports encoding for various IR protocols commonly found in Flipper Zero .ir files.
"""

from enum import Enum
from dataclasses import dataclass
from typing import List, Tuple, Optional


class ProtocolType(Enum):
    """Supported IR Protocol Types"""
    NEC = "NEC"
    NEC42 = "NEC42"
    SAMSUNG32 = "Samsung32"
    RC5 = "RC5"
    RC5X = "RC5X"
    RC6 = "RC6"
    SIRC = "SIRC"
    SIRC15 = "SIRC15"
    SIRC20 = "SIRC20"
    RAW = "RAW"
    UNKNOWN = "UNKNOWN"


@dataclass
class IRSignal:
    """Represents an IR signal with timing data"""
    name: str
    protocol: ProtocolType
    frequency: int  # Carrier frequency in Hz (typically 38000)
    duty_cycle: float  # Duty cycle (typically 0.33)
    timings: List[int]  # List of timings in microseconds (mark/space pairs)
    
    # For parsed protocols
    address: Optional[int] = None
    command: Optional[int] = None
    
    def __post_init__(self):
        if self.frequency == 0:
            self.frequency = 38000
        if self.duty_cycle == 0:
            self.duty_cycle = 0.33


class IRProtocol:
    """IR Protocol Encoder/Decoder"""
    
    # Common timing constants (in microseconds)
    NEC_HEADER_MARK = 9000
    NEC_HEADER_SPACE = 4500
    NEC_BIT_MARK = 560
    NEC_ONE_SPACE = 1690
    NEC_ZERO_SPACE = 560
    
    SAMSUNG_HEADER_MARK = 4500
    SAMSUNG_HEADER_SPACE = 4500
    SAMSUNG_BIT_MARK = 560
    SAMSUNG_ONE_SPACE = 1690
    SAMSUNG_ZERO_SPACE = 560
    
    RC5_BIT_TIME = 889  # Half bit period
    RC5_CARRIER = 36000
    
    RC6_HEADER_MARK = 2666
    RC6_HEADER_SPACE = 889
    RC6_BIT_TIME = 444
    RC6_CARRIER = 36000
    
    SIRC_HEADER_MARK = 2400
    SIRC_HEADER_SPACE = 600
    SIRC_ONE_MARK = 1200
    SIRC_ZERO_MARK = 600
    SIRC_SPACE = 600
    
    @classmethod
    def encode_nec(cls, address: int, command: int, extended: bool = False) -> List[int]:
        """
        Encode NEC protocol signal.
        Standard NEC: 8-bit address + 8-bit command (with inverses)
        Extended NEC: 16-bit address + 8-bit command
        """
        timings = []
        
        # Header
        timings.extend([cls.NEC_HEADER_MARK, cls.NEC_HEADER_SPACE])
        
        if extended:
            # 16-bit address (LSB first)
            for i in range(16):
                timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (address >> i) & 1 else cls.NEC_ZERO_SPACE])
        else:
            # 8-bit address + inverse
            for i in range(8):
                timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (address >> i) & 1 else cls.NEC_ZERO_SPACE])
            addr_inv = (~address) & 0xFF
            for i in range(8):
                timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (addr_inv >> i) & 1 else cls.NEC_ZERO_SPACE])
        
        # 8-bit command + inverse
        for i in range(8):
            timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (command >> i) & 1 else cls.NEC_ZERO_SPACE])
        cmd_inv = (~command) & 0xFF
        for i in range(8):
            timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (cmd_inv >> i) & 1 else cls.NEC_ZERO_SPACE])
        
        # Stop bit
        timings.append(cls.NEC_BIT_MARK)
        
        return timings
    
    @classmethod
    def encode_nec42(cls, address: int, command: int) -> List[int]:
        """Encode NEC42 protocol (42-bit NEC variant)"""
        timings = []
        
        # Header
        timings.extend([cls.NEC_HEADER_MARK, cls.NEC_HEADER_SPACE])
        
        # 13-bit address + 13-bit address (inverted) + 8-bit command + 8-bit command (inverted)
        for i in range(13):
            timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (address >> i) & 1 else cls.NEC_ZERO_SPACE])
        
        addr_inv = (~address) & 0x1FFF
        for i in range(13):
            timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (addr_inv >> i) & 1 else cls.NEC_ZERO_SPACE])
        
        for i in range(8):
            timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (command >> i) & 1 else cls.NEC_ZERO_SPACE])
        
        cmd_inv = (~command) & 0xFF
        for i in range(8):
            timings.extend([cls.NEC_BIT_MARK, cls.NEC_ONE_SPACE if (cmd_inv >> i) & 1 else cls.NEC_ZERO_SPACE])
        
        # Stop bit
        timings.append(cls.NEC_BIT_MARK)
        
        return timings
    
    @classmethod
    def encode_samsung32(cls, address: int, command: int) -> List[int]:
        """Encode Samsung32 protocol"""
        timings = []
        
        # Header (sent twice for Samsung)
        timings.extend([cls.SAMSUNG_HEADER_MARK, cls.SAMSUNG_HEADER_SPACE])
        
        # 8-bit address (MSB first for Samsung)
        for i in range(7, -1, -1):
            timings.extend([cls.SAMSUNG_BIT_MARK, cls.SAMSUNG_ONE_SPACE if (address >> i) & 1 else cls.SAMSUNG_ZERO_SPACE])
        
        # Repeat address
        for i in range(7, -1, -1):
            timings.extend([cls.SAMSUNG_BIT_MARK, cls.SAMSUNG_ONE_SPACE if (address >> i) & 1 else cls.SAMSUNG_ZERO_SPACE])
        
        # 8-bit command (MSB first) + inverse
        for i in range(7, -1, -1):
            timings.extend([cls.SAMSUNG_BIT_MARK, cls.SAMSUNG_ONE_SPACE if (command >> i) & 1 else cls.SAMSUNG_ZERO_SPACE])
        
        cmd_inv = (~command) & 0xFF
        for i in range(7, -1, -1):
            timings.extend([cls.SAMSUNG_BIT_MARK, cls.SAMSUNG_ONE_SPACE if (cmd_inv >> i) & 1 else cls.SAMSUNG_ZERO_SPACE])
        
        # Stop bit
        timings.append(cls.SAMSUNG_BIT_MARK)
        
        return timings
    
    @classmethod
    def encode_rc5(cls, address: int, command: int, toggle: bool = False) -> List[int]:
        """
        Encode RC5 protocol (Philips).
        Uses Manchester encoding with 14 bits total.
        """
        timings = []
        
        # Build 14-bit RC5 frame: S1(1) + S2(1) + Toggle(1) + Address(5) + Command(6)
        bits = []
        
        # Start bits (always 1, 1)
        bits.extend([1, 1])
        
        # Toggle bit
        bits.append(1 if toggle else 0)
        
        # 5-bit address (MSB first)
        for i in range(4, -1, -1):
            bits.append((address >> i) & 1)
        
        # 6-bit command (MSB first)
        for i in range(5, -1, -1):
            bits.append((command >> i) & 1)
        
        return cls._manchester(bits, cls.RC5_BIT_TIME)

    @staticmethod
    def _manchester(bits, half):
        """Bi-phase encode bits into alternating mark/space timings.

        RC5 cells are: 1 = space-then-mark, 0 = mark-then-space. Build the half-bit LEVEL
        stream first, then run-length it -- doing it directly in timings (the old approach)
        got the cell shapes wrong and produced streams with two consecutive same-level
        half-bits, which is not a legal bi-phase cell and decoded as garbage.

        A transmission begins at the first MARK: the leading space of a 1 start bit is just
        silence and is never emitted, so it is dropped here.
        """
        levels = []
        for bit in bits:
            levels.extend([0, 1] if bit else [1, 0])
        i = 0
        while i < len(levels) and levels[i] == 0:
            i += 1
        levels = levels[i:]
        if not levels:
            return []
        timings, cur, run = [], levels[0], 0
        for lv in levels:
            if lv == cur:
                run += 1
            else:
                timings.append(run * half)
                cur, run = lv, 1
        timings.append(run * half)
        return timings

    @classmethod
    def encode_rc5x(cls, address: int, command: int, toggle: bool = False) -> List[int]:
        """Encode RC5X protocol (extended RC5 with 7-bit command)"""
        timings = []
        
        # RC5X is still 14 bits. The 7th command bit is not appended -- it is carried,
        # INVERTED, in the second start bit. Appending it made a 15-bit frame that no
        # receiver (and not our own decoder) would accept.
        bits = [1, 0 if (command >> 6) & 1 else 1]     # S1, S2 = ~cmd[6]
        bits.append(1 if toggle else 0)                # Toggle

        for i in range(4, -1, -1):                     # 5-bit address, MSB first
            bits.append((address >> i) & 1)

        for i in range(5, -1, -1):                     # low 6 command bits, MSB first
            bits.append((command >> i) & 1)
        
        # Same bi-phase rule as RC5; see _manchester for why the level stream is built first.
        return cls._manchester(bits, cls.RC5_BIT_TIME)
    
    @classmethod
    def encode_rc6(cls, address: int, command: int, toggle: bool = False) -> List[int]:
        """Encode RC6 protocol (Philips)"""
        timings = []
        
        # Header
        timings.extend([cls.RC6_HEADER_MARK, cls.RC6_HEADER_SPACE])
        
        # Build frame: Start(1) + Mode(3) + Toggle(1) + Address(8) + Command(8)
        bits = []
        
        # Start bit (always 1)
        bits.append(1)
        
        # Mode bits (000 for standard RC6)
        bits.extend([0, 0, 0])
        
        # Toggle bit
        bits.append(1 if toggle else 0)
        
        # 8-bit address (MSB first)
        for i in range(7, -1, -1):
            bits.append((address >> i) & 1)
        
        # 8-bit command (MSB first)
        for i in range(7, -1, -1):
            bits.append((command >> i) & 1)
        
        # RC6 Manchester encoding (with double-width toggle bit)
        prev_level = 1
        toggle_processed = False
        
        for idx, bit in enumerate(bits):
            bit_time = cls.RC6_BIT_TIME
            
            # Toggle bit (index 4) has double width
            if idx == 4 and not toggle_processed:
                bit_time = cls.RC6_BIT_TIME * 2
                toggle_processed = True
            
            if bit == 1:
                # 1 = high-low
                if prev_level == 1:
                    if len(timings) > 0:
                        timings[-1] += bit_time
                    else:
                        timings.append(bit_time)
                    timings.append(bit_time)
                else:
                    timings.extend([bit_time, bit_time])
                prev_level = 0
            else:
                # 0 = low-high
                if prev_level == 0:
                    if len(timings) > 0:
                        timings[-1] += bit_time
                    else:
                        timings.append(bit_time)
                    timings.append(bit_time)
                else:
                    timings.extend([bit_time, bit_time])
                prev_level = 1
        
        # Final mark
        timings.append(cls.RC6_BIT_TIME)
        
        return timings
    
    @classmethod
    def encode_sirc(cls, address: int, command: int, bits: int = 12) -> List[int]:
        """
        Encode Sony SIRC protocol.
        SIRC12: 7-bit command + 5-bit address
        SIRC15: 7-bit command + 8-bit address
        SIRC20: 7-bit command + 5-bit address + 8-bit extended
        """
        timings = []
        
        # Header
        timings.extend([cls.SIRC_HEADER_MARK, cls.SIRC_HEADER_SPACE])
        
        # 7-bit command (LSB first)
        for i in range(7):
            mark = cls.SIRC_ONE_MARK if (command >> i) & 1 else cls.SIRC_ZERO_MARK
            timings.extend([mark, cls.SIRC_SPACE])
        
        if bits >= 15:
            # 8-bit address (LSB first)
            for i in range(8):
                mark = cls.SIRC_ONE_MARK if (address >> i) & 1 else cls.SIRC_ZERO_MARK
                timings.extend([mark, cls.SIRC_SPACE])
        else:
            # 5-bit address (LSB first)
            for i in range(5):
                mark = cls.SIRC_ONE_MARK if (address >> i) & 1 else cls.SIRC_ZERO_MARK
                timings.extend([mark, cls.SIRC_SPACE])
        
        return timings
    
    # Standard frame PERIOD per protocol: how long one repeat occupies, start of frame to
    # start of the next. The encoders above emit only the frame's own marks and spaces, so
    # encode_signal pads each frame out to its period with a trailing gap.
    #
    # This is not cosmetic. A receiver needs that silence to know one frame ended; without it
    # repeats run together and the device ignores the lot. Verified on hardware: a Sony frame
    # regenerated without its gap (ending in a 600us space instead of ~40000us) produced no
    # reaction at all from a KDL-46W905A, while the same code carrying its gap worked.
    FRAME_PERIOD_US = {
        ProtocolType.NEC: 108000,
        ProtocolType.NEC42: 108000,
        ProtocolType.SAMSUNG32: 108000,
        ProtocolType.RC5: 114000,
        ProtocolType.RC5X: 114000,
        ProtocolType.RC6: 107000,
        ProtocolType.SIRC: 45000,
        ProtocolType.SIRC15: 45000,
        ProtocolType.SIRC20: 45000,
    }
    MIN_FRAME_GAP_US = 10000

    @classmethod
    def _with_frame_gap(cls, timings: List[int], protocol: ProtocolType) -> List[int]:
        """Append the trailing inter-frame gap so the frame fills its standard period."""
        if not timings:
            return timings
        period = cls.FRAME_PERIOD_US.get(protocol)
        if period is None:
            return timings
        gap = period - sum(timings)
        if len(timings) % 2 == 0:
            # frame already ends on a space -- absorb it into the gap rather than adding
            # a second consecutive space, which would corrupt the mark/space alternation
            gap += timings[-1]
            timings = timings[:-1]
        return timings + [max(cls.MIN_FRAME_GAP_US, gap)]

    @classmethod
    def encode_signal(cls, protocol: ProtocolType, address: int, command: int,
                      toggle: bool = False, extended: bool = False) -> IRSignal:
        """Encode an IR signal based on protocol type.

        The returned timings include the protocol's trailing inter-frame gap, so repeating
        them N times produces N properly separated frames.
        """

        if protocol == ProtocolType.NEC:
            timings = cls.encode_nec(address, command, extended)
            freq = 38000
        elif protocol == ProtocolType.NEC42:
            timings = cls.encode_nec42(address, command)
            freq = 38000
        elif protocol == ProtocolType.SAMSUNG32:
            timings = cls.encode_samsung32(address, command)
            freq = 38000
        elif protocol == ProtocolType.RC5:
            timings = cls.encode_rc5(address, command, toggle)
            freq = cls.RC5_CARRIER
        elif protocol == ProtocolType.RC5X:
            timings = cls.encode_rc5x(address, command, toggle)
            freq = cls.RC5_CARRIER
        elif protocol == ProtocolType.RC6:
            timings = cls.encode_rc6(address, command, toggle)
            freq = cls.RC6_CARRIER
        elif protocol == ProtocolType.SIRC:
            timings = cls.encode_sirc(address, command, 12)
            freq = 40000
        elif protocol == ProtocolType.SIRC15:
            timings = cls.encode_sirc(address, command, 15)
            freq = 40000
        elif protocol == ProtocolType.SIRC20:
            timings = cls.encode_sirc(address, command, 20)
            freq = 40000
        else:
            raise ValueError(f"Encoding not supported for protocol: {protocol}")
        
        return IRSignal(
            name=f"{protocol.value}_{address:04X}_{command:04X}",
            protocol=protocol,
            frequency=freq,
            duty_cycle=0.33,
            timings=cls._with_frame_gap(timings, protocol),
            address=address,
            command=command
        )
    
    @classmethod
    def parse_protocol_type(cls, protocol_str: str) -> ProtocolType:
        """Parse protocol type from Flipper Zero .ir file format"""
        protocol_map = {
            "NEC": ProtocolType.NEC,
            "NEC42": ProtocolType.NEC42,
            "Samsung32": ProtocolType.SAMSUNG32,
            "RC5": ProtocolType.RC5,
            "RC5X": ProtocolType.RC5X,
            "RC6": ProtocolType.RC6,
            "SIRC": ProtocolType.SIRC,
            "SIRC15": ProtocolType.SIRC15,
            "SIRC20": ProtocolType.SIRC20,
        }
        return protocol_map.get(protocol_str, ProtocolType.UNKNOWN)