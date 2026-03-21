# Helixd - Core Daemon

**helixd** is the core daemon foundation for the Helix AI Package Manager. The essential daemon infrastructure with Unix socket IPC and basic handlers are implemented.

## Features

- **Fast Startup**: < 1 second startup time
- **Low Memory**: < 30MB idle
- **Unix Socket IPC**: JSON-RPC protocol at `/run/helix/helix.sock`
- **systemd Integration**: Type=notify, watchdog, journald logging
- **Configuration Management**: YAML-based configuration with hot reload
- **Basic IPC Handlers**: ping, version, config, shutdown

## Quick Start

### Recommended: Interactive Setup

```bash
# Run the interactive setup wizard
python scripts/setup_daemon.py
```

The setup wizard will:
1. Check and install required system dependencies (cmake, build-essential, etc.)
2. Build the daemon from source
3. Install the systemd service

### Manual Setup

If you prefer manual installation:

#### 1. Install System Dependencies

```bash
sudo apt-get install -y \
    cmake build-essential libsystemd-dev \
    libssl-dev uuid-dev pkg-config libcap-dev
```

#### 2. Build

```bash
cd daemon
./scripts/build.sh Release
```

#### 3. Install

```bash
sudo ./scripts/install.sh
```

### Verify

```bash
# Check status
systemctl status helixd

# View logs (including startup time)
journalctl -u helixd -f

# Check startup time
journalctl -u helixd | grep "Startup completed"

# Test socket
echo '{"method":"ping"}' | socat - UNIX-CONNECT:/run/helix/helix.sock
```

**Quick startup time check:**
```bash
# Restart and immediately check startup time
sudo systemctl restart helixd && sleep 1 && journalctl -u helixd -n 10 | grep "Startup completed"
```

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                     helix CLI (Python)                      │
└───────────────────────────┬─────────────────────────────────┘
                            │ Unix Socket (/run/helix/helix.sock)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                      helixd (C++)                           │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ IPC Server                                              ││
│  │ ───────────                                             ││
│  │ JSON-RPC Protocol                                       ││
│  │ Basic Handlers: ping, version, config, shutdown         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Config Manager (YAML) │ Logger │ Daemon Lifecycle       ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

## Directory Structure

```text
daemon/
├── include/helixd/          # Public headers
│   ├── common.h              # Types, constants
│   ├── config.h              # Configuration
│   ├── logger.h              # Logging
│   ├── core/                 # Daemon core
│   │   ├── daemon.h
│   │   └── service.h
│   └── ipc/                  # IPC layer
│       ├── server.h
│       ├── protocol.h
│       └── handlers.h        # Basic handlers only
├── src/                      # Implementation
│   ├── core/                 # Daemon lifecycle
│   ├── config/               # Configuration management
│   ├── ipc/                  # IPC server and handlers
│   └── utils/                # Logging utilities
├── systemd/                  # Service files
├── config/                   # Config templates
└── scripts/                  # Build scripts
```

## CLI Commands

Helix provides integrated CLI commands to interact with the daemon:

```bash
# Basic daemon commands
helix daemon ping            # Health check
helix daemon version         # Get daemon version
helix daemon config          # Show configuration
helix daemon reload-config   # Reload configuration
helix daemon shutdown        # Request daemon shutdown

# Install/uninstall daemon
helix daemon install
helix daemon install --execute
helix daemon uninstall
```

```

## IPC API

### Available Methods

| Method | Description |
|--------|-------------|
| `ping` | Health check |
| `version` | Get version info |
| `config.get` | Get configuration |
| `config.reload` | Reload config file |
| `shutdown` | Request shutdown |

### Example

```bash
# Ping the daemon
echo '{"method":"ping"}' | socat - UNIX-CONNECT:/run/helix/helix.sock

# Response:
# {
#   "success": true,
#   "result": {"pong": true}
# }

# Get version
echo '{"method":"version"}' | socat - UNIX-CONNECT:/run/helix/helix.sock

# Response:
# {
#   "success": true,
#   "result": {
#     "version": "1.0.0",
#     "name": "helixd"
#   }
# }

# Get configuration
echo '{"method":"config.get"}' | socat - UNIX-CONNECT:/run/helix/helix.sock
```

## Configuration

Default config: `/etc/helix/daemon.yaml`

```yaml
socket:
  path: /run/helix/helix.sock
  timeout_ms: 5000

log_level: 1  # 0=DEBUG, 1=INFO, 2=WARN, 3=ERROR
```


## Building from Source

### Prerequisites

The easiest way to install all prerequisites is using the setup wizard:

```bash
python scripts/setup_daemon.py
```

The wizard automatically checks and installs these required system packages:

| Package | Purpose |
|---------|---------|
| `cmake` | Build system generator |
| `build-essential` | GCC, G++, make, and other build tools |
| `libsystemd-dev` | systemd integration headers |
| `libssl-dev` | OpenSSL development libraries |
| `uuid-dev` | UUID generation libraries |
| `pkg-config` | Package configuration tool |
| `libcap-dev` | Linux capabilities library |

#### Manual Prerequisite Installation

If you prefer to install dependencies manually:

```bash
# Ubuntu/Debian - Core dependencies
sudo apt-get update
sudo apt-get install -y \
    cmake \
    build-essential \
    libsystemd-dev \
    libssl-dev \
    uuid-dev \
    pkg-config \
    libcap-dev
```

### Build

```bash
# Release build
./scripts/build.sh Release

# Debug build
./scripts/build.sh Debug

# Build with tests
./scripts/build.sh Release --with-tests

# Manual build
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j$(nproc)
```

## Testing

### How Tests Work

Tests run against a **static library** (`helixd_lib`) containing all daemon code, allowing testing without installing the daemon as a systemd service.

```text
┌──────────────────────────────────────────────────────────┐
│                    Test Executable                       │
│                   (e.g., test_config)                    │
└──────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│                    helixd_lib                            │
│          (Static library with all daemon code)           │
│                                                          │
│  • Config, Logger, Daemon, IPCServer, Handlers...        │
│  • Same code that runs in the actual daemon              │
└──────────────────────────────────────────────────────────┘
```

**Key Points:**
- **No daemon installation required** - Tests instantiate classes directly
- **No systemd needed** - Tests run in user space
- **Same code tested** - The library contains identical code to the daemon binary
- **Fast execution** - No service startup overhead

### Test Types

| Type | Purpose | Daemon Required? |
|------|---------|------------------|
| **Unit Tests** | Test individual classes/functions in isolation | No |
| **Integration Tests** | Test component interactions (IPC, handlers) | No |
| **End-to-End Tests** | Test the running daemon service | Yes (not yet implemented) |

### Building Tests

Tests are built separately from the main daemon. Use the `--with-tests` flag:

```bash
./scripts/build.sh Release --with-tests
```

Or use the setup wizard and select "yes" when asked to build tests:

```bash
python scripts/setup_daemon.py
```

### Running Tests

**Using Helix CLI (recommended):**

```bash
# Run all tests
helix daemon run-tests

# Run only unit tests
helix daemon run-tests --unit

# Run only integration tests
helix daemon run-tests --integration

# Run a specific test
helix daemon run-tests --test config
helix daemon run-tests -t daemon

# Verbose output
helix daemon run-tests -v
```

**Using ctest directly:**

```bash
cd daemon/build

# Run all tests
ctest --output-on-failure

# Run specific tests
ctest -R test_config --output-on-failure

# Verbose output
ctest -V
```

### Test Structure

| Test | Type | Description |
|------|------|-------------|
| `test_config` | Unit | Configuration loading and validation |
| `test_protocol` | Unit | IPC message serialization |
| `test_rate_limiter` | Unit | Request rate limiting |
| `test_logger` | Unit | Logging subsystem |
| `test_common` | Unit | Common constants and types |
| `test_ipc_server` | Integration | IPC server lifecycle |
| `test_handlers` | Integration | IPC request handlers |
| `test_daemon` | Integration | Daemon lifecycle and services |

### Example: How Integration Tests Work

```cpp
// test_daemon.cpp - Tests Daemon class without systemd

TEST_F(DaemonTest, InitializeWithValidConfig) {
    // Instantiate Daemon directly (no systemd)
    auto& daemon = helixd::Daemon::instance();
    
    // Call methods and verify behavior
    daemon.initialize(config_path_);
    EXPECT_TRUE(daemon.is_initialized());
    
    // Test config was loaded
    auto config = daemon.config();
    EXPECT_EQ(config.socket_path, expected_path);
}
```

The test creates a temporary config file, instantiates the `Daemon` class directly in memory, and verifies its behavior - all without touching systemd or installing anything.

## systemd Management

```bash
# Start daemon
sudo systemctl start helixd

# Stop daemon
sudo systemctl stop helixd

# View status
sudo systemctl status helixd

# View logs
journalctl -u helixd -f

# Reload config
sudo systemctl reload helixd

# Enable at boot
sudo systemctl enable helixd
```

## Performance

| Metric | Target | Actual |
|--------|--------|--------|
| Startup time | < 1s | 100μs |
| Idle memory | < 30MB | ~700KB |
| Socket latency | < 50ms | ~5-15ms |

### Measuring Startup Time

The daemon automatically measures and logs its startup time on each start. The measurement begins when `Daemon::run()` is called and ends when all services are started and systemd is notified (READY=1).

**What's measured:**
- Service initialization
- IPC server startup
- Handler registration
- systemd notification

**Target:** < 1 second

#### Method 1: Check Daemon Logs (Recommended)

The daemon logs startup time directly in its log output:

```bash
# View recent logs
journalctl -u helixd -n 20

# Look for the startup time message:
# [INFO] Daemon: Startup completed in XXXms (or XXXμs for very fast startups)

# Or filter for startup messages only
journalctl -u helixd | grep "Startup completed"
```

**Example output:**
```text
[INFO] Daemon: Starting daemon
[INFO] Daemon: Starting service: IPCServer
[INFO] IPCServer: Started on /run/helix/helix.sock
[INFO] Daemon: Service started: IPCServer
[INFO] Daemon: Startup completed in 234.567ms
[INFO] Daemon: Daemon started successfully
```

**Note:** For very fast startups (< 1ms), the time is shown in microseconds (μs) for precision:
```text
[INFO] Daemon: Startup completed in 456μs
```
#### Method 2: Manual Timing with systemctl

Time the service start manually:

```bash
# Stop the service first
sudo systemctl stop helixd

# Time the start command
time sudo systemctl start helixd

# Check if it's running
systemctl is-active helixd
```


### Measuring Idle Memory

The daemon should use less than 30MB of memory when idle (no active requests).

**Target:** < 30MB

#### Method 1: Using systemctl status

```bash
# Check current memory usage
systemctl status helixd

# Look for the "Memory:" line in the output
# Example: Memory: 24.5M
```

#### Method 2: Using ps

```bash
# Check memory usage with ps
ps aux | grep helixd | grep -v grep

# Or get just the RSS (Resident Set Size) in MB
ps -o pid,rss,comm -p $(pgrep helixd) | awk 'NR>1 {print $2/1024 " MB"}'
```

#### Method 3: Using systemd-cgls

```bash
# Check memory usage via cgroup
systemctl show helixd -p MemoryCurrent

# Output is in bytes, convert to MB:
# MemoryCurrent=25165824 (bytes) = ~24MB
```

**Note:** Ensure the daemon is idle (no active IPC requests) when measuring. Memory usage may temporarily spike during request handling, but should return to baseline when idle.

### Measuring Socket Latency

Socket latency is the time it takes for a request to travel from client to daemon and back (round-trip time).

**Target:** < 50ms

#### Method 1: Using time with socat

```bash
# Measure latency of a ping request
time echo '{"method":"ping"}' | socat - UNIX-CONNECT:/run/helix/helix.sock

# The "real" time shows the total round-trip latency
```

#### Method 2: Using time with helix CLI

```bash
# If helix CLI is available, time a command
time helix daemon ping
```

**Note:** Socket latency can vary based on system load. For accurate measurement:
- Run when system is idle
- Take multiple measurements and average
- Ensure daemon is running and responsive

## Security

- Unix socket with 0666 permissions (local access only, not network accessible)
- No network exposure
- systemd hardening (NoNewPrivileges, ProtectSystem, etc.)
- Minimal attack surface (core daemon only)

## Contributing

1. Follow C++17 style
2. Add tests for new features
3. Update documentation
4. Test on Ubuntu 22.04+

## License

Apache 2.0 - See [LICENSE](../LICENSE)

## Support
