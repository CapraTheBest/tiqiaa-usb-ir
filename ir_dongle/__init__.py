"""
USB-C IR Dongle Python Library
A library for interfacing with USB-C Infrared dongles on Windows.
Supports Flipper Zero .ir file format for IR signal transmission.
"""

__version__ = "0.1.0"
__author__ = "CapraTheBest"

from .device import IRDongle, find_dongle, find_tiqiaa_dongle
from .transmitter import IRTransmitter
from .flipper_parser import FlipperIRParser
from .protocols import IRProtocol, ProtocolType
from .tiqiaa_protocol import TiqiaaProtocol

__all__ = [
    "IRDongle",
    "find_dongle",
    "find_tiqiaa_dongle",
    "IRTransmitter",
    "FlipperIRParser",
    "IRProtocol",
    "ProtocolType",
    "TiqiaaProtocol",
    "decode_blob",
]