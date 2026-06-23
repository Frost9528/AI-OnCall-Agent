package org.example.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class SessionCleanupTask {

    private static final Logger logger = LoggerFactory.getLogger(SessionCleanupTask.class);

    private final SessionManager sessionManager;

    public SessionCleanupTask(SessionManager sessionManager) {
        this.sessionManager = sessionManager;
    }

    @Scheduled(cron = "0 0 3 * * ?")
    public void cleanupExpiredSessions() {
        logger.info("开始清理过期会话...");
        sessionManager.deleteExpiredSessions();
    }
}
