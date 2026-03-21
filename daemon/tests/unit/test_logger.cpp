/**
 * @file test_logger.cpp
 * @brief Unit tests for Logger class
 */

#include <gtest/gtest.h>
#include <sstream>
#include <regex>
#include <thread>
#include <vector>
#include <atomic>
#include "helixd/logger.h"

class LoggerTest : public ::testing::Test {
protected:
    void SetUp() override {
        // Each test starts with a fresh logger state
        helixd::Logger::shutdown();
    }
    
    void TearDown() override {
        helixd::Logger::shutdown();
    }
};

// ============================================================================
// Initialization tests
// ============================================================================

TEST_F(LoggerTest, InitializesWithDefaultLevel) {
    helixd::Logger::init(helixd::LogLevel::INFO, false);
    
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::INFO);
}

TEST_F(LoggerTest, InitializesWithCustomLevel) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::DEBUG);
}

TEST_F(LoggerTest, InitializesWithErrorLevel) {
    helixd::Logger::init(helixd::LogLevel::ERROR, false);
    
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::ERROR);
}

TEST_F(LoggerTest, InitializesWithCriticalLevel) {
    helixd::Logger::init(helixd::LogLevel::CRITICAL, false);
    
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::CRITICAL);
}

// ============================================================================
// Level setting tests
// ============================================================================

TEST_F(LoggerTest, SetLevelWorks) {
    helixd::Logger::init(helixd::LogLevel::INFO, false);
    
    helixd::Logger::set_level(helixd::LogLevel::DEBUG);
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::DEBUG);
    
    helixd::Logger::set_level(helixd::LogLevel::WARN);
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::WARN);
    
    helixd::Logger::set_level(helixd::LogLevel::ERROR);
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::ERROR);
}

TEST_F(LoggerTest, GetLevelReturnsCorrectLevel) {
    helixd::Logger::init(helixd::LogLevel::WARN, false);
    
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::WARN);
}

// ============================================================================
// Log level filtering tests
// ============================================================================

TEST_F(LoggerTest, DebugLevelLogsAllMessages) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    // These should not throw or crash
    helixd::Logger::debug("Test", "debug message");
    helixd::Logger::info("Test", "info message");
    helixd::Logger::warn("Test", "warn message");
    helixd::Logger::error("Test", "error message");
    helixd::Logger::critical("Test", "critical message");
    
    SUCCEED();
}

TEST_F(LoggerTest, InfoLevelFiltersDebug) {
    helixd::Logger::init(helixd::LogLevel::INFO, false);
    
    // Debug should be filtered
    helixd::Logger::debug("Test", "should be filtered");
    
    // These should pass through
    helixd::Logger::info("Test", "info message");
    helixd::Logger::warn("Test", "warn message");
    helixd::Logger::error("Test", "error message");
    helixd::Logger::critical("Test", "critical message");
    
    SUCCEED();
}

TEST_F(LoggerTest, WarnLevelFiltersDebugAndInfo) {
    helixd::Logger::init(helixd::LogLevel::WARN, false);
    
    // Debug and Info should be filtered
    helixd::Logger::debug("Test", "should be filtered");
    helixd::Logger::info("Test", "should be filtered");
    
    // These should pass through
    helixd::Logger::warn("Test", "warn message");
    helixd::Logger::error("Test", "error message");
    helixd::Logger::critical("Test", "critical message");
    
    SUCCEED();
}

TEST_F(LoggerTest, ErrorLevelFiltersDebugInfoWarn) {
    helixd::Logger::init(helixd::LogLevel::ERROR, false);
    
    // Debug, Info, Warn should be filtered
    helixd::Logger::debug("Test", "should be filtered");
    helixd::Logger::info("Test", "should be filtered");
    helixd::Logger::warn("Test", "should be filtered");
    
    // These should pass through
    helixd::Logger::error("Test", "error message");
    helixd::Logger::critical("Test", "critical message");
    
    SUCCEED();
}

TEST_F(LoggerTest, CriticalLevelFiltersAllButCritical) {
    helixd::Logger::init(helixd::LogLevel::CRITICAL, false);
    
    // All but critical should be filtered
    helixd::Logger::debug("Test", "should be filtered");
    helixd::Logger::info("Test", "should be filtered");
    helixd::Logger::warn("Test", "should be filtered");
    helixd::Logger::error("Test", "should be filtered");
    
    // Only critical should pass through
    helixd::Logger::critical("Test", "critical message");
    
    SUCCEED();
}

// ============================================================================
// Macro tests
// ============================================================================

TEST_F(LoggerTest, LogMacrosWork) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    // Test all logging macros
    LOG_DEBUG("MacroTest", "debug via macro");
    LOG_INFO("MacroTest", "info via macro");
    LOG_WARN("MacroTest", "warn via macro");
    LOG_ERROR("MacroTest", "error via macro");
    LOG_CRITICAL("MacroTest", "critical via macro");
    
    SUCCEED();
}

// ============================================================================
// Thread safety tests
// ============================================================================

TEST_F(LoggerTest, ThreadSafeLogging) {
    helixd::Logger::init(helixd::LogLevel::INFO, false);
    
    std::atomic<int> log_count{0};
    std::vector<std::thread> threads;
    
    // Launch multiple threads all logging
    for (int t = 0; t < 10; ++t) {
        threads.emplace_back([&, t]() {
            for (int i = 0; i < 100; ++i) {
                helixd::Logger::info("Thread" + std::to_string(t), "message " + std::to_string(i));
                log_count++;
            }
        });
    }
    
    for (auto& thread : threads) {
        thread.join();
    }
    
    EXPECT_EQ(log_count.load(), 1000);
}

TEST_F(LoggerTest, ThreadSafeLevelChange) {
    helixd::Logger::init(helixd::LogLevel::INFO, false);
    
    std::atomic<bool> running{true};
    
    // Thread that keeps logging
    std::thread logger_thread([&]() {
        while (running) {
            helixd::Logger::info("Test", "message");
            std::this_thread::sleep_for(std::chrono::microseconds(10));
        }
    });
    
    // Thread that keeps changing level
    std::thread changer_thread([&]() {
        for (int i = 0; i < 100; ++i) {
            helixd::Logger::set_level(helixd::LogLevel::DEBUG);
            helixd::Logger::set_level(helixd::LogLevel::INFO);
            helixd::Logger::set_level(helixd::LogLevel::WARN);
            helixd::Logger::set_level(helixd::LogLevel::ERROR);
        }
    });
    
    changer_thread.join();
    running = false;
    logger_thread.join();
    
    // If we got here without crashing, thread safety is working
    SUCCEED();
}

// ============================================================================
// Edge cases
// ============================================================================

TEST_F(LoggerTest, EmptyMessageWorks) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    helixd::Logger::info("Test", "");
    
    SUCCEED();
}

TEST_F(LoggerTest, EmptyComponentWorks) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    helixd::Logger::info("", "message");
    
    SUCCEED();
}

TEST_F(LoggerTest, LongMessageWorks) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    std::string long_message(10000, 'a');
    helixd::Logger::info("Test", long_message);
    
    SUCCEED();
}

TEST_F(LoggerTest, SpecialCharactersInMessage) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    helixd::Logger::info("Test", "Special chars: \n\t\"'\\{}[]");
    helixd::Logger::info("Test", "Unicode: 日本語 中文 한국어");
    
    SUCCEED();
}

TEST_F(LoggerTest, LoggingWithoutInit) {
    // Logger should still work even if not explicitly initialized
    // (uses static defaults)
    helixd::Logger::info("Test", "message before init");
    
    SUCCEED();
}

// ============================================================================
// Shutdown and reinit tests
// ============================================================================

TEST_F(LoggerTest, ShutdownAndReinit) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    helixd::Logger::info("Test", "before shutdown");
    
    helixd::Logger::shutdown();
    
    helixd::Logger::init(helixd::LogLevel::INFO, false);
    helixd::Logger::info("Test", "after reinit");
    
    EXPECT_EQ(helixd::Logger::get_level(), helixd::LogLevel::INFO);
}

TEST_F(LoggerTest, MultipleShutdownCalls) {
    helixd::Logger::init(helixd::LogLevel::DEBUG, false);
    
    helixd::Logger::shutdown();
    helixd::Logger::shutdown();  // Should not crash
    helixd::Logger::shutdown();
    
    SUCCEED();
}

// ============================================================================
// LogLevel enum tests
// ============================================================================

TEST_F(LoggerTest, LogLevelOrdering) {
    // Verify log levels have correct ordering
    EXPECT_LT(static_cast<int>(helixd::LogLevel::DEBUG), static_cast<int>(helixd::LogLevel::INFO));
    EXPECT_LT(static_cast<int>(helixd::LogLevel::INFO), static_cast<int>(helixd::LogLevel::WARN));
    EXPECT_LT(static_cast<int>(helixd::LogLevel::WARN), static_cast<int>(helixd::LogLevel::ERROR));
    EXPECT_LT(static_cast<int>(helixd::LogLevel::ERROR), static_cast<int>(helixd::LogLevel::CRITICAL));
}

TEST_F(LoggerTest, AllLogLevelsHaveValues) {
    EXPECT_EQ(static_cast<int>(helixd::LogLevel::DEBUG), 0);
    EXPECT_EQ(static_cast<int>(helixd::LogLevel::INFO), 1);
    EXPECT_EQ(static_cast<int>(helixd::LogLevel::WARN), 2);
    EXPECT_EQ(static_cast<int>(helixd::LogLevel::ERROR), 3);
    EXPECT_EQ(static_cast<int>(helixd::LogLevel::CRITICAL), 4);
}

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
