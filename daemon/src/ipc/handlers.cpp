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
#include <cstdlib>
#include <memory>
#include <sstream>
#include <string>
#include <sys/wait.h>

namespace {

std::string trim(const std::string& s) {
    size_t start = s.find_first_not_of(" \t\n\r");
    if (start == std::string::npos) {
        return "";
    }
    size_t end = s.find_last_not_of(" \t\n\r");
    return s.substr(start, end - start + 1);
}

std::string shell_escape_single_quoted(const std::string& input) {
    std::string out;
    out.reserve(input.size() + 8);
    out.push_back('\'');
    for (char c : input) {
        if (c == '\'') {
            out += "'\\''";
        } else {
            out.push_back(c);
        }
    }
    out.push_back('\'');
    return out;
}

bool run_command_capture_stdout(const std::string& command, std::string& output, int& exit_code) {
    output.clear();
    exit_code = -1;

    std::array<char, 4096> buffer{};
    std::string wrapped = "bash -lc " + shell_escape_single_quoted(command);

    std::unique_ptr<FILE, int (*)(FILE*)> pipe(popen(wrapped.c_str(), "r"), pclose);
    if (!pipe) {
        return false;
    }

    while (fgets(buffer.data(), static_cast<int>(buffer.size()), pipe.get()) != nullptr) {
        output.append(buffer.data());
    }

    int status = pclose(pipe.release());
    if (status == -1) {
        return false;
    }

    if (WIFEXITED(status)) {
        exit_code = WEXITSTATUS(status);
    } else {
        exit_code = -1;
    }
    return true;
}

bool detect_package_manager(std::string& pm) {
    int code = -1;
    std::string out;

    if (run_command_capture_stdout("command -v apt-get >/dev/null 2>&1", out, code) && code == 0) {
        pm = "apt";
        return true;
    }
    if (run_command_capture_stdout("command -v dnf >/dev/null 2>&1", out, code) && code == 0) {
        pm = "dnf";
        return true;
    }
    if (run_command_capture_stdout("command -v yum >/dev/null 2>&1", out, code) && code == 0) {
        pm = "yum";
        return true;
    }
    if (run_command_capture_stdout("command -v zypper >/dev/null 2>&1", out, code) && code == 0) {
        pm = "zypper";
        return true;
    }
    if (run_command_capture_stdout("command -v pacman >/dev/null 2>&1", out, code) && code == 0) {
        pm = "pacman";
        return true;
    }
    return false;
}

int count_non_empty_lines(const std::string& text) {
    std::istringstream iss(text);
    std::string line;
    int count = 0;
    while (std::getline(iss, line)) {
        if (!trim(line).empty()) {
            ++count;
        }
    }
    return count;
}

}

namespace helixd {

void Handlers::register_all(IPCServer& server) {
    // Basic handlers only
    server.register_handler(Methods::PING, [](const Request& req) {
        return handle_ping(req);
    });
    
    server.register_handler(Methods::VERSION, [](const Request& req) {
        return handle_version(req);
    });

    // Security handlers
    server.register_handler(Methods::ALERTS_GET, [](const Request& req) {
        return handle_alerts_get(req);
    });
    server.register_handler(Methods::SECURITY_PATCHES_INSTALL, [](const Request& req) {
        return handle_security_patches_install(req);
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
    
    LOG_INFO("Handlers", "Registered 8 IPC handlers");
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

Response Handlers::handle_alerts_get(const Request& /*req*/) {
    std::string package_manager;
    if (!detect_package_manager(package_manager)) {
        return Response::err("Could not detect package manager", ErrorCodes::INTERNAL_ERROR);
    }

    std::string list_cmd;
    if (package_manager == "apt") {
        list_cmd = "apt list --upgradable 2>/dev/null | tail -n +2 | grep -i -- '-security\\|security' || true";
    } else if (package_manager == "dnf") {
        list_cmd = "dnf -q updateinfo list security 2>/dev/null | tail -n +1 || true";
    } else if (package_manager == "yum") {
        list_cmd = "yum -q updateinfo list security 2>/dev/null | tail -n +1 || true";
    } else if (package_manager == "zypper") {
        list_cmd = "zypper -q list-patches --category security 2>/dev/null | tail -n +1 || true";
    } else if (package_manager == "pacman") {
        list_cmd = "checkupdates 2>/dev/null || true";
    } else {
        return Response::err("Unsupported package manager", ErrorCodes::INTERNAL_ERROR);
    }

    std::string stdout_text;
    int exit_code = -1;
    if (!run_command_capture_stdout(list_cmd, stdout_text, exit_code)) {
        return Response::err("Failed to query security updates", ErrorCodes::INTERNAL_ERROR);
    }

    int missing_count = count_non_empty_lines(stdout_text);
    bool has_alert = missing_count > 0;

    json alerts = json::array();
    if (has_alert) {
        alerts.push_back({
            {"id", "security-updates-missing"},
            {"severity", "warning"},
            {"title", "Security updates are missing"},
            {"message", "System has pending security patches. Install them to reduce risk."},
            {"missing_security_updates", missing_count},
            {"details", trim(stdout_text)}
        });
    }

    return Response::ok({
        {"package_manager", package_manager},
        {"has_alerts", has_alert},
        {"alerts", alerts},
        {"missing_security_updates", missing_count}
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

Response Handlers::handle_security_patches_install(const Request& /*req*/) {
    std::string package_manager;
    if (!detect_package_manager(package_manager)) {
        return Response::err("Could not detect package manager", ErrorCodes::INTERNAL_ERROR);
    }

    // Run security update installation through sudo in non-interactive mode.
    // If sudo requires a password, this will fail gracefully with a useful error.
    std::string install_cmd;
    if (package_manager == "apt") {
        install_cmd = "sudo -n apt-get update && sudo -n apt-get upgrade -y";
    } else if (package_manager == "dnf") {
        install_cmd = "sudo -n dnf upgrade --security -y";
    } else if (package_manager == "yum") {
        install_cmd = "sudo -n yum update --security -y";
    } else if (package_manager == "zypper") {
        install_cmd = "sudo -n zypper --non-interactive patch --category security";
    } else if (package_manager == "pacman") {
        install_cmd = "sudo -n pacman -Syu --noconfirm";
    } else {
        return Response::err("Unsupported package manager", ErrorCodes::INTERNAL_ERROR);
    }

    std::string stdout_text;
    int exit_code = -1;
    if (!run_command_capture_stdout(install_cmd + " 2>&1", stdout_text, exit_code)) {
        return Response::err("Failed to execute security patch command", ErrorCodes::INTERNAL_ERROR);
    }

    if (exit_code != 0) {
        return Response::err(
            "Security patch installation failed (ensure sudo access). Output: " + trim(stdout_text),
            ErrorCodes::INTERNAL_ERROR
        );
    }

    return Response::ok({
        {"installed", true},
        {"package_manager", package_manager},
        {"output", trim(stdout_text)}
    });
}

} // namespace helixd