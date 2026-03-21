/**
 * @file handlers.cpp
 * @brief IPC request handler implementations
 */

#include "helixd/ipc/handlers.h"
#include "helixd/core/daemon.h"
#include "helixd/config.h"
#include "helixd/logger.h"
#include <array>
#include <cstdio>
#include <sstream>
#include <string>

namespace helixd {

void Handlers::register_all(IPCServer& server) {
    // Basic handlers only
    server.register_handler(Methods::PING, [](const Request& req) {
        return handle_ping(req);
    });
    
    server.register_handler(Methods::VERSION, [](const Request& req) {
        return handle_version(req);
    });
    
    // Config handlers
    server.register_handler(Methods::CONFIG_GET, [](const Request& req) {
        return handle_config_get(req);
    });
    
    server.register_handler(Methods::CONFIG_RELOAD, [](const Request& req) {
        return handle_config_reload(req);
    });
    
    // Package management
    server.register_handler(Methods::PACKAGES_ANALYZE, [](const Request& req) {
        return handle_packages_analyze(req);
    });

    // Daemon control
    server.register_handler(Methods::SHUTDOWN, [](const Request& req) {
        return handle_shutdown(req);
    });

    LOG_INFO("Handlers", "Registered 6 core IPC handlers");
}

Response Handlers::handle_ping(const Request& /*req*/) {
    return Response::ok({{"pong", true}});
}

Response Handlers::handle_version(const Request& /*req*/) {
    return Response::ok({
        {"version", VERSION},
        {"name", NAME}
    });
}

Response Handlers::handle_config_get(const Request& /*req*/) {
    const auto& config = ConfigManager::instance().get();
    
    // PR 1: Return only core daemon configuration
    json result = {
        {"socket_path", config.socket_path},
        {"socket_backlog", config.socket_backlog},
        {"socket_timeout_ms", config.socket_timeout_ms},
        {"max_requests_per_sec", config.max_requests_per_sec},
        {"log_level", config.log_level}
    };
    
    return Response::ok(result);
}

Response Handlers::handle_config_reload(const Request& /*req*/) {
    if (Daemon::instance().reload_config()) {
        return Response::ok({{"reloaded", true}});
    }
    return Response::err("Failed to reload configuration", ErrorCodes::CONFIG_ERROR);
}

Response Handlers::handle_packages_analyze(const Request& /*req*/) {
    LOG_INFO("Handlers", "Analyzing outdated packages");

    // Run apt list --upgradable and capture output
    std::array<char, 256> buffer;
    std::string output;

    FILE* pipe = popen("apt list --upgradable 2>/dev/null", "r");
    if (!pipe) {
        return Response::err("Failed to run package analysis", ErrorCodes::INTERNAL_ERROR);
    }

    while (fgets(buffer.data(), buffer.size(), pipe) != nullptr) {
        output += buffer.data();
    }
    pclose(pipe);

    // Parse output lines: "package/source version_new arch [upgradable from: version_old]"
    json packages = json::array();
    std::istringstream stream(output);
    std::string line;

    while (std::getline(stream, line)) {
        // Skip header line "Listing..."
        if (line.find("Listing") != std::string::npos || line.empty()) {
            continue;
        }

        // Extract package name (before '/')
        auto slash_pos = line.find('/');
        if (slash_pos == std::string::npos) continue;
        std::string name = line.substr(0, slash_pos);

        // Extract latest version (after space, before next space)
        auto space1 = line.find(' ', slash_pos);
        if (space1 == std::string::npos) continue;
        auto space2 = line.find(' ', space1 + 1);
        std::string latest = line.substr(space1 + 1, space2 - space1 - 1);

        // Extract current version (after "upgradable from: " and before "]")
        std::string current = "unknown";
        auto from_pos = line.find("upgradable from: ");
        if (from_pos != std::string::npos) {
            auto ver_start = from_pos + 17;
            auto ver_end = line.find(']', ver_start);
            if (ver_end != std::string::npos) {
                current = line.substr(ver_start, ver_end - ver_start);
            }
        }

        packages.push_back({
            {"name", name},
            {"current_version", current},
            {"latest_version", latest}
        });
    }

    LOG_INFO("Handlers", "Found " + std::to_string(packages.size()) + " outdated package(s)");
    return Response::ok({{"packages", packages}});
}

Response Handlers::handle_shutdown(const Request& /*req*/) {
    LOG_INFO("Handlers", "Shutdown requested via IPC");
    Daemon::instance().request_shutdown();
    return Response::ok({{"shutdown", "initiated"}});
}

} // namespace helixd