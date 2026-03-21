/**
 * @file test_common.cpp
 * @brief Unit tests for common.h constants and types (PR1 scope only)
 * 
 * PR1 includes: Core daemon, IPC server, config management
 */

#include <gtest/gtest.h>
#include "helixd/common.h"

class CommonTest : public ::testing::Test {
protected:
    void SetUp() override {}
    void TearDown() override {}
};

// ============================================================================
// Version and Name constants (PR1)
// ============================================================================

TEST_F(CommonTest, VersionIsDefined) {
    EXPECT_NE(helixd::VERSION, nullptr);
    EXPECT_STRNE(helixd::VERSION, "");
}

TEST_F(CommonTest, NameIsDefined) {
    EXPECT_NE(helixd::NAME, nullptr);
    EXPECT_STREQ(helixd::NAME, "helixd");
}

// ============================================================================
// Socket constants (PR1 - used by IPC server)
// ============================================================================

TEST_F(CommonTest, DefaultSocketPathIsDefined) {
    EXPECT_NE(helixd::DEFAULT_SOCKET_PATH, nullptr);
    EXPECT_STREQ(helixd::DEFAULT_SOCKET_PATH, "/run/helix/helix.sock");
}

TEST_F(CommonTest, SocketBacklogIsPositive) {
    EXPECT_GT(helixd::SOCKET_BACKLOG, 0);
}

TEST_F(CommonTest, SocketTimeoutIsPositive) {
    EXPECT_GT(helixd::SOCKET_TIMEOUT_MS, 0);
}

TEST_F(CommonTest, MaxMessageSizeIsPositive) {
    EXPECT_GT(helixd::MAX_MESSAGE_SIZE, 0);
    // Should be at least 1KB for reasonable messages
    EXPECT_GE(helixd::MAX_MESSAGE_SIZE, 1024);
}

// ============================================================================
// Startup time target (PR1 - daemon startup performance)
// ============================================================================

TEST_F(CommonTest, StartupTimeTargetIsDefined) {
    EXPECT_GT(helixd::STARTUP_TIME_MS, 0);
    // Should be reasonable (less than 10 seconds)
    EXPECT_LT(helixd::STARTUP_TIME_MS, 10000);
}

// ============================================================================
// Clock type alias (PR1 - used in IPC protocol)
// ============================================================================

TEST_F(CommonTest, ClockTypeAliasIsDefined) {
    // Verify Clock is a valid type alias
    helixd::Clock::time_point now = helixd::Clock::now();
    EXPECT_GT(now.time_since_epoch().count(), 0);
}

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
