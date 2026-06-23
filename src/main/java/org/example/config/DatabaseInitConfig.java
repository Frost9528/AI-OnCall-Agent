package org.example.config;

import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

@Configuration
public class DatabaseInitConfig {

    private static final Logger logger = LoggerFactory.getLogger(DatabaseInitConfig.class);

    private final JdbcTemplate jdbcTemplate;

    public DatabaseInitConfig(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    @PostConstruct
    public void init() {
        logger.info("初始化数据库表结构...");
        createServiceAlertsTable();
        createIncidentRecordsTable();
        createKnowledgeBaseTable();
        insertSampleData();
        logger.info("数据库初始化完成");
    }

    private void createServiceAlertsTable() {
        jdbcTemplate.execute("""
            CREATE TABLE IF NOT EXISTS service_alerts (
                id VARCHAR(64) PRIMARY KEY,
                alert_name VARCHAR(128) NOT NULL,
                service_name VARCHAR(128),
                severity VARCHAR(32),
                status VARCHAR(32),
                alert_time TIMESTAMP,
                description TEXT
            )
        """);
    }

    private void createIncidentRecordsTable() {
        jdbcTemplate.execute("""
            CREATE TABLE IF NOT EXISTS incident_records (
                id VARCHAR(64) PRIMARY KEY,
                alert_name VARCHAR(128),
                handler VARCHAR(64),
                action TEXT,
                duration_minutes INT,
                resolved_at TIMESTAMP
            )
        """);
    }

    private void createKnowledgeBaseTable() {
        jdbcTemplate.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id VARCHAR(64) PRIMARY KEY,
                title VARCHAR(256),
                category VARCHAR(64),
                content TEXT,
                source_doc VARCHAR(128)
            )
        """);
    }

    private void insertSampleData() {
        // 只插入一次
        Integer count = jdbcTemplate.queryForObject("SELECT COUNT(*) FROM service_alerts", Integer.class);
        if (count != null && count > 0) {
            logger.info("数据库已有数据，跳过样例数据插入");
            return;
        }

        jdbcTemplate.update("INSERT INTO service_alerts (id, alert_name, service_name, severity, status, description) VALUES (?, ?, ?, ?, ?, ?)",
                "1", "HighCPUUsage", "payment-service", "CRITICAL", "FIRING",
                "CPU 使用率持续超过 80%，当前值 92%");
        jdbcTemplate.update("INSERT INTO service_alerts (id, alert_name, service_name, severity, status, description) VALUES (?, ?, ?, ?, ?, ?)",
                "2", "HighMemoryUsage", "order-service", "CRITICAL", "FIRING",
                "内存使用率持续超过 85%，当前值 91%");

        jdbcTemplate.update("INSERT INTO incident_records (id, alert_name, handler, action, duration_minutes) VALUES (?, ?, ?, ?, ?)",
                "1", "HighCPUUsage", "张三", "扩容 payment-service 实例数 2→4", 30);
        jdbcTemplate.update("INSERT INTO incident_records (id, alert_name, handler, action, duration_minutes) VALUES (?, ?, ?, ?, ?)",
                "2", "HighMemoryUsage", "李四", "优化 order-service JVM 参数", 45);

        jdbcTemplate.update("INSERT INTO knowledge_base (id, title, category, content, source_doc) VALUES (?, ?, ?, ?, ?)",
                "1", "CPU 告警处理流程", "告警处理", "步骤1: 确认异常Pod; 步骤2: 检查负载; 步骤3: 扩容或优化代码", "cpu_high_usage.md");
        jdbcTemplate.update("INSERT INTO knowledge_base (id, title, category, content, source_doc) VALUES (?, ?, ?, ?, ?)",
                "2", "内存告警处理流程", "告警处理", "步骤1: 检查JVM堆内存; 步骤2: 分析GC日志; 步骤3: 检查内存泄漏", "memory_high_usage.md");

        logger.info("样例数据插入完成");
    }
}
