"""
CLI Interface for USB-C IR Dongle
Command-line tool for sending IR signals and managing the dongle.
"""

import argparse
import sys
from typing import Optional
from .device import list_available_devices, find_dongle
from .transmitter import IRTransmitter, list_ir_files
from .flipper_parser import FlipperIRParser


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="USB-C IR Dongle CLI - Send IR signals from your PC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ir send --file remote.ir --signal POWER
  ir send --nec 0x04 0x08
  ir list-devices
  ir list-signals --file remote.ir
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Send command
    send_parser = subparsers.add_parser("send", help="Send an IR signal")
    send_parser.add_argument("--file", "-f", help="Path to Flipper Zero .ir file")
    send_parser.add_argument("--signal", "-s", help="Signal name to send from .ir file")
    send_parser.add_argument("--nec", nargs=2, metavar=("ADDR", "CMD"), 
                             help="Send NEC signal with address and command (hex)")
    send_parser.add_argument("--samsung", nargs=2, metavar=("ADDR", "CMD"),
                             help="Send Samsung32 signal with address and command (hex)")
    send_parser.add_argument("--rc5", nargs=2, metavar=("ADDR", "CMD"),
                             help="Send RC5 signal with address and command (hex)")
    send_parser.add_argument("--rc6", nargs=2, metavar=("ADDR", "CMD"),
                             help="Send RC6 signal with address and command (hex)")
    send_parser.add_argument("--repeat", "-r", type=int, default=1,
                             help="Number of times to repeat the signal (default: 1)")
    send_parser.add_argument("--vid", "-v", help="Vendor ID filter (hex, e.g., 0x04D8)")
    send_parser.add_argument("--pid", "-p", help="Product ID filter (hex, e.g., 0x0001)")
    
    # List devices command
    list_dev_parser = subparsers.add_parser("list-devices", aliases=["ld"], 
                                            help="List available USB devices")
    
    # List signals command
    list_sig_parser = subparsers.add_parser("list-signals", aliases=["ls"],
                                            help="List signals in an .ir file")
    list_sig_parser.add_argument("--file", "-f", required=True, help="Path to .ir file")
    
    # List IR files command
    list_files_parser = subparsers.add_parser("list-files", aliases=["lf"],
                                              help="List .ir files in a directory")
    list_files_parser.add_argument("--directory", "-d", default=".", help="Directory to search")
    
    # Info command
    info_parser = subparsers.add_parser("info", help="Show device information")
    info_parser.add_argument("--vid", "-v", help="Vendor ID filter (hex)")
    info_parser.add_argument("--pid", "-p", help="Product ID filter (hex)")
    
    # Raw send command (for debugging)
    raw_parser = subparsers.add_parser("raw", help="Send raw IR timings")
    raw_parser.add_argument("--freq", "-f", type=int, default=38000, help="Carrier frequency (Hz)")
    raw_parser.add_argument("--duty", "-d", type=float, default=0.33, help="Duty cycle (0.0-1.0)")
    raw_parser.add_argument("timings", nargs="+", type=int, help="Mark/space timings in microseconds")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Parse VID/PID if provided
    vid = int(args.vid, 16) if hasattr(args, 'vid') and args.vid else None
    pid = int(args.pid, 16) if hasattr(args, 'pid') and args.pid else None
    
    # Execute command
    if args.command == "list-devices" or args.command == "ld":
        return cmd_list_devices()
    
    elif args.command == "list-signals" or args.command == "ls":
        return cmd_list_signals(args.file)
    
    elif args.command == "list-files" or args.command == "lf":
        return cmd_list_files(args.directory)
    
    elif args.command == "info":
        return cmd_info(vid, pid)
    
    elif args.command == "send":
        return cmd_send(args, vid, pid)
    
    elif args.command == "raw":
        return cmd_raw(args, vid, pid)
    
    return 0


def cmd_list_devices() -> int:
    """List available USB devices"""
    devices = list_available_devices()
    if not devices:
        print("\nNo devices found. Make sure your IR dongle is plugged in.")
        print("Try running find_dongle.ps1 for more detailed detection.")
    return 0


def cmd_list_signals(filepath: str) -> int:
    """List signals in an IR file"""
    try:
        parser = FlipperIRParser()
        signals = parser.parse_file(filepath)
        
        print(f"Signals in '{filepath}':")
        print("-" * 40)
        for signal in signals:
            protocol = signal.protocol.value
            if signal.address is not None and signal.command is not None:
                details = f"  Address: 0x{signal.address:04X}, Command: 0x{signal.command:04X}"
            else:
                details = f"  Timings: {len(signal.timings)} values"
            print(f"  {signal.name}")
            print(f"    Protocol: {protocol}")
            print(details)
            print(f"    Frequency: {signal.frequency} Hz")
            print()
        
        return 0
    except FileNotFoundError:
        print(f"Error: File not found: {filepath}")
        return 1
    except Exception as e:
        print(f"Error parsing file: {e}")
        return 1


def cmd_list_files(directory: str) -> int:
    """List IR files in a directory"""
    files = list_ir_files(directory)
    if not files:
        print(f"No .ir files found in '{directory}'")
        return 0
    
    print(f"Found {len(files)} .ir file(s) in '{directory}':")
    print("-" * 40)
    for f in files:
        print(f"  {f}")
    return 0


def cmd_info(vid: Optional[int], pid: Optional[int]) -> int:
    """Show device information"""
    print("Searching for IR dongle...")
    dongle = find_dongle(vid=vid, pid=pid)
    
    if not dongle:
        print("No IR dongle found.")
        return 1
    
    info = dongle.device_info
    print("\nDevice Information:")
    print("-" * 40)
    print(f"  Description:   {info.description}")
    print(f"  VID:PID:       {info.vid_pid_str}")
    print(f"  Device Type:   {info.device_type.value}")
    print(f"  Path:          {info.path}")
    if info.manufacturer:
        print(f"  Manufacturer:  {info.manufacturer}")
    if info.product:
        print(f"  Product:       {info.product}")
    if info.serial_number:
        print(f"  Serial:        {info.serial_number}")
    
    print("\nAttempting to connect...")
    if dongle.open():
        print("Connection successful!")
        dongle.close()
    else:
        print("Connection failed. Check permissions and drivers.")
        return 1
    
    return 0


def cmd_send(args, vid: Optional[int], pid: Optional[int]) -> int:
    """Send an IR signal"""
    with IRTransmitter(vid=vid, pid=pid) as tx:
        if not tx.connected:
            print("Error: Could not connect to IR dongle")
            return 1
        
        print(f"Connected to IR dongle")
        
        # Send from file
        if args.file:
            tx.load_ir_file(args.file)
            if not args.signal:
                print(f"Loaded {len(tx.get_signal_names())} signals. Specify --signal to send.")
                return 0
            
            print(f"Sending signal: {args.signal} (repeat: {args.repeat})")
            if tx.send(args.signal, args.repeat):
                print("Signal sent successfully!")
                return 0
            else:
                print("Failed to send signal")
                return 1
        
        # Send protocol signals
        elif args.nec:
            addr = int(args.nec[0], 16)
            cmd = int(args.nec[1], 16)
            print(f"Sending NEC: Address=0x{addr:02X}, Command=0x{cmd:02X}")
            if tx.send_nec(addr, cmd, args.repeat):
                print("Signal sent successfully!")
                return 0
        
        elif args.samsung:
            addr = int(args.samsung[0], 16)
            cmd = int(args.samsung[1], 16)
            print(f"Sending Samsung32: Address=0x{addr:02X}, Command=0x{cmd:02X}")
            if tx.send_samsung(addr, cmd, args.repeat):
                print("Signal sent successfully!")
                return 0
        
        elif args.rc5:
            addr = int(args.rc5[0], 16)
            cmd = int(args.rc5[1], 16)
            print(f"Sending RC5: Address=0x{addr:02X}, Command=0x{cmd:02X}")
            if tx.send_rc5(addr, cmd, args.repeat):
                print("Signal sent successfully!")
                return 0
        
        elif args.rc6:
            addr = int(args.rc6[0], 16)
            cmd = int(args.rc6[1], 16)
            print(f"Sending RC6: Address=0x{addr:02X}, Command=0x{cmd:02X}")
            if tx.send_rc6(addr, cmd, args.repeat):
                print("Signal sent successfully!")
                return 0
        
        else:
            print("Error: Specify --file with --signal, or use --nec/--samsung/--rc5/--rc6")
            return 1
    
    return 0


def cmd_raw(args, vid: Optional[int], pid: Optional[int]) -> int:
    """Send raw IR timings"""
    with IRTransmitter(vid=vid, pid=pid) as tx:
        if not tx.connected:
            print("Error: Could not connect to IR dongle")
            return 1
        
        print(f"Sending raw signal: {len(args.timings)} timings @ {args.freq} Hz")
        if tx.send_raw(args.timings, args.freq, args.duty):
            print("Signal sent successfully!")
            return 0
        else:
            print("Failed to send signal")
            return 1


if __name__ == "__main__":
    sys.exit(main())