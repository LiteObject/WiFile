# WiFile

A simple command-line tool for transferring files over a WiFi network using TCP sockets. WiFile allows you to quickly send files between devices on the same network without requiring cloud services or external dependencies.

## Features

- **Simple file transfer** - Send files directly between devices
- **Folder transfer** - Send all files in a folder (including subfolders) one by one
- **Network-based** - Works over any TCP/IP network (WiFi, Ethernet, etc.)
- **Any file type** - Transfer any file regardless of format or size
- **Command-line interface** - Easy to use from terminal/command prompt
- **Lightweight** - Pure Python with no external dependencies
- **Real-time progress bar** - Visual transfer progress with speed and ETA
- **Automatic IP display** - Server shows its IP address for easy connection
- **Smart file conflict handling** - Options to overwrite, rename, or cancel
- **Robust error handling** - Graceful handling of network interruptions
- **Connection timeouts** - Prevents hanging on network issues
- **Persistent server** - Keeps serving transfers; resend the same file/folder or switch to a new one after each transfer

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

If you run `python wifile.py server` without `--file` or `--folder`, the
server prompts you to enter a file or folder path before it starts listening.

### Server Mode (Sending a folder)

Send all files inside a folder (including subfolders) one by one over a single connection:

```bash
python wifile.py server --folder /path/to/your/folder
```

Files are transferred sequentially in a batch. The client receives each file
and recreates the folder structure in its output directory.

Optional parameters:
- `--port`: Specify a custom port (default: 12345)

Examples:
```bash
python wifile.py server --file document.pdf --port 8080
python wifile.py server --folder ./photos --port 8080
```

> **Persistent server**: the server does **not** exit after a transfer. After
> each transfer it prompts for what to serve next:
> - `s` - send the same file(s)/folder again
> - `n` - provide a new file or folder path to send
> - `e` - exit the server
>
> Press `Ctrl+C` in the server terminal to stop at any time.

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
| `--file` | Path to the file to send | No (prompted if omitted) | - |
| `--folder` | Path to the folder whose contents to send one by one (recurses into subfolders) | No (prompted if omitted) | - |
| `--port` | Port number to listen on | No | 12345 |

### Client Mode
| Option | Description | Required | Default |
|--------|-------------|----------|---------|
| `--host` | IP address of the server | Yes | - |
| `--port` | Port number to connect to | No | 12345 |
| `--output-dir` | Directory to save received file(s) | No | Current directory |
| `--overwrite` | Automatically overwrite existing files | No | False |
| `--auto-rename` | Automatically rename if file exists | No | False |

## Folder Transfer (Batch Mode)

When the server runs with `--folder`, it sends every file inside the folder
(recursively) **one by one over a single connection**, using a framed wire
protocol: each control message is `1-byte type + 4-byte length + payload`.
The server announces the file count, then for every file it sends a header
(name + size), the client resolves any name conflicts and signals it is
ready, the server streams the file, and the client confirms the file was
written before the next one starts. Subfolder structure is preserved on the
receiving side. No action is needed on the client - it automatically detects
a batch transfer and keeps receiving until done.

## How It Works

1. **Server** starts listening on a specified port and waits for connections
2. **Server** automatically displays its IP address for easy client connection
3. **Client** connects to the server using the server's IP address and port
4. **Server** sends a framed file header (name and size)
5. **Client** resolves any file name conflicts, then acknowledges it is ready
6. **Server** transmits the file content in 1KB chunks with real-time progress
7. **Both sides** show progress bars with transfer speed and estimated time remaining
8. **Client** receives and saves the file, then confirms completion to the server

For folder transfers, steps 4-8 repeat for each file in the folder. The server
first announces how many files it will send, then sends each file sequentially
with a per-file completion confirmation, and finally signals the end of the
batch. Subfolder structure is preserved on the receiving side. No action is
needed on the client - it automatically detects a batch transfer and keeps
receiving until done.

After a client disconnects (transfer finished or aborted), the server asks
whether to send the same file(s)/folder again, switch to a new file or folder,
or quit. Run it in its own terminal and press `Ctrl+C` to stop it.

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

> `--overwrite` and `--auto-rename` are mutually exclusive; passing both is an
error. Likewise, `--file` and `--folder` cannot be combined on the server.

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

## Contributing

Feel free to submit issues, fork the repository, and create pull requests for any improvements.

## License

This project is open source. Please check the repository for license details.
