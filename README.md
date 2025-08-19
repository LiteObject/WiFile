# WiFile

A simple command-line tool for transferring files over a WiFi network using TCP sockets. WiFile allows you to quickly send files between devices on the same network without requiring cloud services or external dependencies.

## Features

- **Simple file transfer** - Send files directly between devices
- **Network-based** - Works over any TCP/IP network (WiFi, Ethernet, etc.)
- **Any file type** - Transfer any file regardless of format or size
- **Command-line interface** - Easy to use from terminal/command prompt
- **Lightweight** - Pure Python with no external dependencies

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

Example:
```bash
python wifile.py client --host 192.168.1.100 --port 8080 --output-dir ./downloads
```

## Complete Example

### Step 1: Start the server (on the sending device)
```bash
python wifile.py server --file myfile.zip
```
Output:
```
Server listening on port 12345...
```

### Step 2: Connect with client (on the receiving device)
```bash
python wifile.py client --host 192.168.1.50
```
Output:
```
Connected to server 192.168.1.50:12345
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

## How It Works

1. **Server** starts listening on a specified port and waits for connections
2. **Client** connects to the server using the server's IP address and port
3. **Server** sends the filename and file size first
4. **Server** then transmits the file content in 1KB chunks
5. **Client** receives and saves the file to the specified output directory

## Network Discovery

To find the IP address of the server device:

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

**Important**: This tool is designed for use on trusted local networks only.

- No encryption or authentication is implemented
- Files are transmitted in plain text
- Only use on trusted networks (home, office)
- Firewall may need to be configured to allow connections

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

## Contributing

Feel free to submit issues, fork the repository, and create pull requests for any improvements.

## License

This project is open source. Please check the repository for license details.
