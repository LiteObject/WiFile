# WiFile

A simple command-line tool for transferring files over a WiFi network using TCP sockets. WiFile allows you to quickly send files between devices on the same network without requiring cloud services or external dependencies.

## Features

- 🚀 **Simple file transfer** - Send files directly between devices
- 🌐 **Network-based** - Works over any TCP/IP network (WiFi, Ethernet, etc.)
- 📁 **Any file type** - Transfer any file regardless of format or size
- 🔧 **Command-line interface** - Easy to use from terminal/command prompt
- 🛡️ **Lightweight** - Pure Python with no external dependencies
- 📊 **Real-time progress bar** - Visual transfer progress with speed and ETA
- 🆔 **Automatic IP display** - Server shows its IP address for easy connection
- 🔄 **Smart file conflict handling** - Options to overwrite, rename, or cancel
- ⚡ **Robust error handling** - Graceful handling of network interruptions
- ⏱️ **Connection timeouts** - Prevents hanging on network issues

## Requirements

- Python 3.x
- Network connectivity between sender and receiver

## Installation

1. Clone or download the repository
2. Ensure Python 3.x is installed on your system
3. No additional dependencies required - uses only Python standard library

## Usage

WiFile operates in two modes: **server** (sender) and **client** (receiver).

### Server Mode (Sending a file)

Run this on the device that has the file you want to send:

```bash
python wifile.py server --file /path/to/your/file.txt
```

Optional parameters:
- `--port`: Specify a custom port (default: 12345)

Example:
```bash
python wifile.py server --file document.pdf --port 8080
```

### Client Mode (Receiving a file)

Run this on the device that will receive the file:

```bash
python wifile.py client --host <server-ip-address>
```

Optional parameters:
- `--port`: Specify the port (must match server port, default: 12345)
- `--output-dir`: Specify where to save the received file (default: current directory)
- `--overwrite`: Automatically overwrite existing files without prompting
- `--auto-rename`: Automatically rename files if they already exist

Examples:
```bash
python wifile.py client --host 192.168.1.100 --port 8080 --output-dir ./downloads
python wifile.py client --host 192.168.1.100 --overwrite
python wifile.py client --host 192.168.1.100 --auto-rename
```

## Complete Example

### Step 1: Start the server (on the sending device)
```bash
python wifile.py server --file myfile.zip
```
Output:
```
Server listening on port 12345
Server IP address: 192.168.1.50
Clients can connect using: python wifile.py client --host 192.168.1.50
Waiting for connection...
Connected by ('192.168.1.100', 55124)
Sending 'myfile.zip' (2.3 MB)...
|████████████████████████████████████████████████████| 100.0% (2.3 MB/2.3 MB) - 1.2 MB/s - ETA: 0s
File 'myfile.zip' sent successfully.
```

### Step 2: Connect with client (on the receiving device)
```bash
python wifile.py client --host 192.168.1.50
```
Output:
```
Connected to server 192.168.1.50:12345
Receiving 'myfile.zip' (2.3 MB)...
|████████████████████████████████████████████████████| 100.0% (2.3 MB/2.3 MB) - 1.2 MB/s - ETA: 0s
File 'myfile.zip' received and saved to './myfile.zip'.
```

## Command-line Options

### Server Mode
| Option | Description | Required | Default |
|--------|-------------|----------|---------|
| `--file` | Path to the file to send | Yes | - |
| `--port` | Port number to listen on | No | 12345 |

### Client Mode
| Option | Description | Required | Default |
|--------|-------------|----------|---------|
| `--host` | IP address of the server | Yes | - |
| `--port` | Port number to connect to | No | 12345 |
| `--output-dir` | Directory to save received file | No | Current directory |
| `--overwrite` | Automatically overwrite existing files | No | False |
| `--auto-rename` | Automatically rename if file exists | No | False |

## How It Works

1. **Server** starts listening on a specified port and waits for connections
2. **Server** automatically displays its IP address for easy client connection
3. **Client** connects to the server using the server's IP address and port
4. **Server** sends the filename and file size with proper protocol handshaking
5. **Client** acknowledges receipt and handles any file name conflicts
6. **Server** transmits the file content in 1KB chunks with real-time progress
7. **Both sides** show progress bars with transfer speed and estimated time remaining
8. **Client** receives and saves the file to the specified output directory

## File Conflict Handling

When a file with the same name already exists on the client side, WiFile provides three options:

### Interactive Mode (Default)
```
Warning: File 'document.pdf' already exists in './downloads'
Choose action: (o)verwrite, (r)ename, (c)ancel: r
Saving as 'document_1.pdf' instead...
```

### Automatic Modes
- **`--overwrite`**: Automatically replace existing files
- **`--auto-rename`**: Automatically rename to avoid conflicts (file_1.ext, file_2.ext, etc.)

## Progress Bar Features

WiFile shows real-time transfer progress with:
- **Visual progress bar** with completion percentage
- **File size information** in human-readable format (KB, MB, GB)
- **Transfer speed** in real-time (e.g., "1.2 MB/s")
- **Estimated time remaining** (ETA)
- **Automatic IP detection** - no need to manually find server IP address

## Network Discovery

WiFile automatically displays the server's IP address when it starts, but you can also find it manually:

**Windows:**
```cmd
ipconfig
```

**macOS/Linux:**
```bash
ifconfig
# or
ip addr show
```

Look for the IP address under your WiFi adapter (usually starts with 192.168.x.x or 10.x.x.x for local networks).

## Security Considerations

⚠️ **Important**: This tool is designed for use on trusted local networks only.

- No encryption or authentication is implemented
- Files are transmitted in plain text
- Only use on trusted networks (home, office)
- Firewall may need to be configured to allow connections
- 30-second connection timeouts help prevent hanging connections

## Troubleshooting

### Common Issues

**"Connection refused"**
- Ensure the server is running before starting the client
- Check that both devices are on the same network
- Verify the IP address and port are correct
- Check firewall settings

**"File does not exist"**
- Verify the file path is correct
- Use absolute paths if relative paths don't work

**"Permission denied"**
- Ensure you have read permissions for the source file
- Ensure you have write permissions for the output directory

**"Connection lost during transfer"**
- Check network stability
- Ensure both devices stay connected to the same network
- Try again - WiFile will show exactly how much data was transferred

**"UTF-8 codec can't decode"**
- This was an issue in older versions, now fixed with improved protocol
- Update to the latest version if you encounter this

**"File already exists" prompts**
- Use `--overwrite` to automatically replace files
- Use `--auto-rename` to automatically rename conflicting files
- Or respond to the interactive prompt with 'o', 'r', or 'c'

## Recent Improvements

### Version 2.0 Features
- ✅ **Real-time progress bars** with speed and ETA information
- ✅ **Automatic IP address detection and display** for easy client connection
- ✅ **Smart file conflict handling** with interactive and automatic modes
- ✅ **Robust error handling** for network interruptions and connection issues
- ✅ **Improved protocol** with proper handshaking to prevent data corruption
- ✅ **Connection timeouts** to prevent hanging on network issues
- ✅ **Better error messages** with specific guidance for common problems
- ✅ **Transfer resumption information** showing exactly how much data was transferred

### Code Quality
- 🔧 **10/10 pylint rating** - high code quality standards
- 🧪 **Comprehensive error handling** for all network scenarios
- 📚 **Detailed documentation** and examples
- 🚀 **Performance optimized** with efficient chunked transfers

## Contributing

Feel free to submit issues, fork the repository, and create pull requests for any improvements.

## License

This project is open source. Please check the repository for license details.
