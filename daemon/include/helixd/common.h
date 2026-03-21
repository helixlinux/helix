/**
 * @file common.h
 * @brief Common types and constants for helixd
 */

#pragma once

#include <chrono>
#include <nlohmann/json.hpp>

namespace helixd {

// JSON type alias
using json = nlohmann::json;

// Version info - HELIXD_VERSION is defined by CMake from PROJECT_VERSION
#ifndef HELIXD_VERSION
#define HELIXD_VERSION "1.0.0"  // Fallback for non-CMake builds
#endif
constexpr const char* VERSION = HELIXD_VERSION;
constexpr const char* NAME = "helixd";

// Socket constants
constexpr const char* DEFAULT_SOCKET_PATH = "/run/helix/helix.sock";
constexpr int SOCKET_BACKLOG = 16;
constexpr int SOCKET_TIMEOUT_MS = 5000;
constexpr size_t MAX_MESSAGE_SIZE = 65536;  // 64KB

// Performance targets
constexpr int STARTUP_TIME_MS = 1000;  // Target: < 1 second startup time

// Clock type alias for consistency (used in IPC protocol)
using Clock = std::chrono::system_clock;

} // namespace helixd
