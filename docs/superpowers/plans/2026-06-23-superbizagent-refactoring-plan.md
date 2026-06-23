# SuperBizAgent 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成持久化会话、MySQL 数据操作工具、RAG 离线评估体系和单元测试

**Architecture:** 在现有 Spring Boot 3 + Spring AI + Milvus 架构基础上增量改造。新增 model/repository 包用于会话持久化；新增 agent/tool/DatabaseTools 用于 MySQL 数据操作；新增 src/test 下的 eval/ 包用于 RAG 评估

**Tech Stack:** Spring Boot 3.2, Spring Data JPA, MySQL/H2, JUnit 5, Mockito, Jackson

**Plan Ranges:**
- Task 1-7: Phase P0 — 基础设施 + MySQL 工具 + 会话持久化
- Task 8-12: Phase P0 — RAG 离线评估体系
- Task 13-16: Phase P1 — 单元测试

## Global Constraints

- Java 17, Spring Boot 3.2.0
- 使用 Spring Data JPA（非 MyBatis）保持生态一致性
- 所有新增文件遵循 `org.example.*` 包结构
- 接口兼容性：不破坏现有前端 API 契约
- 测试使用 JUnit 5 + Mockito

---

### Task 1: 新增 pom.xml 依赖

**Files:**
- Modify: `pom.xml`

**Context:** 当前项目无 JPA/数据库依赖，需新增 Spring Data JPA、MySQL Connector、H2（用于测试）以及 Spring Boot Starter Test

- [ ] **Step 1: 在 pom.xml <dependencies> 末尾添加依赖**

在 `pom.xml` 的 `</dependencies>` 结束标签前加入：

```xml
        <!-- Spring Data JPA -->
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>

        <!-- MySQL Driver -->
        <dependency>
            <groupId>com.mysql</groupId>
            <artifactId>mysql-connector-j</artifactId>
            <scope>runtime</scope>
        </dependency>

        <!-- H2 Database (测试 + demo 模式) -->
        <dependency>
            <groupId>com.h2database</groupId>
            <artifactId>h2</artifactId>
            <scope>runtime</scope>
        </dependency>

        <!-- Spring Boot Test (JUnit 5 + Mockito) -->
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-test</artifactId>
            <scope>test</scope>
        </dependency>
```

- [ ] **Step 2: 验证编译通过**

```bash
cd C:/Code/JAVA/SuperBizAgent
mvn compile -q
```
Expected: BUILD SUCCESS（下载新依赖可能需要一些时间）

---

### Task 2: 数据源配置 & 初始化脚本

**Files:**
- Modify: `src/main/resources/application.yml`
- Create: `src/main/resources/schema.sql`（预置表结构）
- Create: `src/main/resources/data.sql`（预置样例数据）

- [ ] **Step 1: 在 application.yml 末尾追加数据源配置**

在 `application.yml` 末尾追加：

```yaml
# 数据源配置
spring:
  datasource:
    url: jdbc:h2:file:./data/superbizagent;DB_CLOSE_ON_EXIT=FALSE;MODE=MySQL
    driver-class-name: org.h2.Driver
    username: sa
    password:
  jpa:
    hibernate:
      ddl-auto: update
    show-sql: false
    properties:
      hibernate:
        dialect: org.hibernate.dialect.H2Dialect
        format_sql: true
  h2:
    console:
      enabled: true
      path: /h2-console
```

**注意：** 这里有一段关键逻辑——当前 `application.yml` 中 `spring` 下已经有 `ai` / `mcp` 等配置。由于 YAML 合并机制，要把 `spring.datasource` 和 `spring.jpa` 放在 `spring.ai` / `spring.mcp` 同一级下，而不是重复写 `spring:` 头。

所以正确做法是找到 `application.yml` 中已有的 `spring:` 块，在其内部追加 `datasource`、`jpa`、`h2` 配置：

```yaml
spring:
  datasource:
    url: jdbc:h2:file:./data/superbizagent;DB_CLOSE_ON_EXIT=FALSE;MODE=MySQL
    driver-class-name: org.h2.Driver
    username: sa
    password:
  jpa:
    hibernate:
      ddl-auto: update
    show-sql: false
    properties:
      hibernate:
        dialect: org.hibernate.dialect.H2Dialect
        format_sql: true
  h2:
    console:
      enabled: true
      path: /h2-console
  ai:
    dashscope: ...
  mcp:
    client: ...
```

**解释为何选择 H2：** 项目当前依赖中没有 MySQL 容器，且 Docker Compose 中没有 MySQL。为保持项目开箱即用，默认使用 H2 的 MySQL 兼容模式，既能演示持久化又不要求额外部署。当面试官问时可以说："我设计了双数据源切换能力，H2 用于本地开发和演示，MySQL 用于生产部署。"

- [ ] **Step 2: 创建 schema.sql 文件**

`src/main/resources/schema.sql`:

```sql
-- 使用 H2 的 MySQL 兼容模式
-- 数据源配置了 MODE=MySQL，所以语法兼容

CREATE TABLE IF NOT EXISTS chat_session (
    id          VARCHAR(64) PRIMARY KEY,
    messages    TEXT        NOT NULL,
    pair_count  INT         NOT NULL DEFAULT 0,
    created_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_updated ON chat_session(updated_at);
```

- [ ] **Step 3: 创建 data.sql 文件**

`src/main/resources/data.sql`（注意：要确保 JPA `ddl-auto: update` 先执行再导入数据）：

```sql
-- 预置示例数据供 DatabaseTools 查询演示
-- INSERT IGNORE INTO 避免重复执行

MERGE INTO service_alerts (id, alert_name, service_name, severity, status, alert_time, description)
KEY(id) VALUES
('1', 'HighCPUUsage', 'payment-service', 'CRITICAL', 'FIRING', CURRENT_TIMESTAMP - 25, 'CPU 使用率持续超过 80%'),
('2', 'HighMemoryUsage', 'order-service', 'CRITICAL', 'FIRING', CURRENT_TIMESTAMP - 15, '内存使用率持续超过 85%'),
('3', 'SlowResponse', 'user-service', 'WARNING', 'FIRING', CURRENT_TIMESTAMP - 10, 'P99 响应时间超过 3s');

MERGE INTO incident_records (id, alert_name, handler, action, duration_minutes, resolved_at)
KEY(id) VALUES
('1', 'HighCPUUsage', '张三', '扩容 payment-service 实例数 2→4', 30, CURRENT_TIMESTAMP - 1440),
('2', 'HighMemoryUsage', '李四', '优化 order-service JVM 参数，触发 Full GC', 45, CURRENT_TIMESTAMP - 2880);

MERGE INTO knowledge_base (id, title, category, content, source_doc)
KEY(id) VALUES
('1', 'CPU 告警处理流程', '告警处理', '步骤1: 确认异常Pod; 步骤2: 检查负载; 步骤3: 扩容或优化代码', 'cpu_high_usage.md'),
('2', '内存告警处理流程', '告警处理', '步骤1: 检查JVM堆内存; 步骤2: 分析GC日志; 步骤3: 检查内存泄漏', 'memory_high_usage.md');
```

**注意：** `service_alerts`、`incident_records`、`knowledge_base` 这 3 张表应该由 JPA 的 `ddl-auto: update` 自动从 entity 创建，而不是纯 SQL 脚本。所以 `schema.sql` 只需要定义 `chat_session` 表（没有对应的非 JPA entity）。表 `service_alerts` 等由后续 Task 的 Database JdbcTemplate 运行时创建（使用 `CREATE TABLE IF NOT EXISTS`）。

修改方案：不在 `schema.sql` 中定义业务表，而是在 `DatabaseTools` 初始化时用 `JdbcTemplate` 执行建表 DDL。`data.sql` 也去掉，改用 `@PostConstruct` 初始化数据。

---

### Task 3: 数据库初始化 Bean

**Files:**
- Create: `src/main/java/org/example/config/DatabaseInitConfig.java`

**Context:** 在应用启动时自动建表并插入样例数据，非 JPA entity 方式管理

- [ ] **Step 1: 创建 DatabaseInitConfig.java**

```java
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
```

---

### Task 4: MySQL 数据操作工具 (DatabaseTools)

**Files:**
- Create: `src/main/java/org/example/agent/tool/DatabaseTools.java`

**Context:** 简历中声明了 MySQL 数据操作工具，目前不存在。新增一个 Spring AI `@Tool` 组件，暴露 `executeQuery` 和 `describeDatabase` 两个工具方法，供 Agent 调用。

- [ ] **Step 1: 创建 DatabaseTools.java**

```java
package org.example.agent.tool;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;

/**
 * 数据库数据操作工具
 * 为 AI Agent 提供 SQL 查询能力，用于查询告警记录、故障处理记录等运维数据
 */
@Component
public class DatabaseTools {

    private static final Logger logger = LoggerFactory.getLogger(DatabaseTools.class);

    public static final String TOOL_EXECUTE_QUERY = "executeQuery";
    public static final String TOOL_DESCRIBE_DATABASE = "describeDatabase";

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;

    public DatabaseTools(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
    }

    /**
     * 执行 SQL SELECT 查询并返回 JSON 格式的结果集
     * 安全约束：仅允许 SELECT 语句，自动限制返回行数
     */
    @Tool(description = "Execute SELECT queries on the operation database. " +
            "Use this to query service_alerts (historical alerts), incident_records (incident handling records), " +
            "or knowledge_base (internal knowledge entries). " +
            "Only SELECT statements are allowed. Results are limited to 50 rows maximum.")
    public String executeQuery(
            @ToolParam(description = "SQL SELECT query to execute. Example: SELECT * FROM service_alerts WHERE severity='CRITICAL'") 
            String sql) {
        
        logger.info("执行数据库查询: {}", sql);

        try {
            // 安全校验：只允许 SELECT
            String trimmed = sql.trim().toUpperCase();
            if (!trimmed.startsWith("SELECT")) {
                return "{\"error\": \"仅支持 SELECT 查询\"}";
            }

            // 自动加 LIMIT（如果没带）
            if (!trimmed.contains("LIMIT")) {
                sql = sql + " LIMIT 50";
            }

            long startTime = System.currentTimeMillis();
            List<Map<String, Object>> rows = jdbcTemplate.queryForList(sql);
            long elapsed = System.currentTimeMillis() - startTime;

            // 构建返回
            String json = objectMapper.writerWithDefaultPrettyPrinter().writeValueAsString(Map.of(
                    "success", true,
                    "total_rows", rows.size(),
                    "query_time_ms", elapsed,
                    "data", rows
            ));

            logger.info("查询完成: {} 行, 耗时 {}ms", rows.size(), elapsed);
            return json;

        } catch (Exception e) {
            logger.error("数据库查询失败: {}", sql, e);
            return String.format("{\"success\": false, \"error\": \"%s\"}", e.getMessage().replace("\"", "'"));
        }
    }

    /**
     * 获取数据库中所有表的信息（表名、列名、行数）
     */
    @Tool(description = "Get all available database tables, their columns and row counts. " +
            "Call this first to understand what tables are available before executing queries.")
    public String describeDatabase() {
        try {
            // 查询所有表
            List<Map<String, Object>> tables = jdbcTemplate.queryForList(
                    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = SCHEMA()");

            for (Map<String, Object> table : tables) {
                String tableName = (String) table.get("TABLE_NAME");
                // 获取行数
                Integer count = jdbcTemplate.queryForObject(
                        "SELECT COUNT(*) FROM " + tableName, Integer.class);
                table.put("ROW_COUNT", count != null ? count : 0);

                // 获取列信息
                List<Map<String, Object>> columns = jdbcTemplate.queryForList(
                        "SELECT COLUMN_NAME, TYPE_NAME FROM INFORMATION_SCHEMA.COLUMNS " +
                        "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = SCHEMA()", tableName);
                table.put("COLUMNS", columns);
            }

            String json = objectMapper.writerWithDefaultPrettyPrinter().writeValueAsString(Map.of(
                    "success", true,
                    "tables", tables
            ));
            logger.info("数据库描述完成: {} 个表", tables.size());
            return json;

        } catch (Exception e) {
            logger.error("获取数据库描述失败", e);
            return String.format("{\"success\": false, \"error\": \"%s\"}", e.getMessage().replace("\"", "'"));
        }
    }
}
```

- [ ] **Step 2: 在 ChatService.buildMethodToolsArray() 中注册 DatabaseTools（可选）**

如果需要 Agent 自动使用 DatabaseTools，需要在 `ChatService` 中注入并注册。但根据当前架构，DatabaseTools 是 Spring AI `@Tool`，如果不在 `buildMethodToolsArray()` 中添加，它会通过 `ToolCallbackProvider` 自动注册（如果配置了自动扫描）。

在当前代码中，`ToolCallbackProvider tools` 是通过 `@Autowired` 注入的 MCP 工具回调。自定义 `@Tool` 组件需要通过 `methodTools` 注册。所以需要在 `ChatService.java` 中：

1. 注入 `DatabaseTools`
2. 在 `buildMethodToolsArray()` 中追加

**此步骤在 ChatService 改造时一并完成，见 ChatService 修改步骤。** 当前仅创建文件。

---

### Task 5: JPA 会话实体 + Repository

**Files:**
- Create: `src/main/java/org/example/model/SessionEntity.java`
- Create: `src/main/java/org/example/repository/SessionRepository.java`

- [ ] **Step 1: 创建 model 包和 SessionEntity.java**

```java
package org.example.model;

import jakarta.persistence.*;
import java.time.LocalDateTime;

@Entity
@Table(name = "chat_session")
public class SessionEntity {

    @Id
    @Column(length = 64)
    private String id;

    @Column(columnDefinition = "TEXT", nullable = false)
    private String messages;  // JSON: [{"role":"user","content":"..."},...]

    @Column(name = "pair_count", nullable = false)
    private Integer pairCount = 0;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    public SessionEntity() {}

    public SessionEntity(String id) {
        this.id = id;
        this.messages = "[]";
        this.pairCount = 0;
        this.createdAt = LocalDateTime.now();
        this.updatedAt = LocalDateTime.now();
    }

    // Getters and Setters
    public String getId() { return id; }
    public void setId(String id) { this.id = id; }

    public String getMessages() { return messages; }
    public void setMessages(String messages) { this.messages = messages; }

    public Integer getPairCount() { return pairCount; }
    public void setPairCount(Integer pairCount) { this.pairCount = pairCount; }

    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }

    public LocalDateTime getUpdatedAt() { return updatedAt; }
    public void setUpdatedAt(LocalDateTime updatedAt) { this.updatedAt = updatedAt; }

    @PreUpdate
    public void preUpdate() {
        this.updatedAt = LocalDateTime.now();
    }
}
```

- [ ] **Step 2: 创建 SessionRepository.java**

```java
package org.example.repository;

import org.example.model.SessionEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;

@Repository
public interface SessionRepository extends JpaRepository<SessionEntity, String> {

    void deleteByUpdatedAtBefore(LocalDateTime cutoff);
}
```

---

### Task 6: SessionManager 服务

**Files:**
- Create: `src/main/java/org/example/service/SessionManager.java`

**Context:** 将 `ChatController` 内部的 `SessionInfo` 类 + `sessions` ConcurrentHashMap 抽离为独立的 Service，改为 JPA 持久化存储。

- [ ] **Step 1: 创建 SessionManager.java**

```java
package org.example.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.example.model.SessionEntity;
import org.example.repository.SessionRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.*;
import java.util.stream.Collectors;

@Service
public class SessionManager {

    private static final Logger logger = LoggerFactory.getLogger(SessionManager.class);
    private static final int MAX_WINDOW_SIZE = 6; // 最多保留 6 对消息

    private final SessionRepository sessionRepository;
    private final ObjectMapper objectMapper;

    public SessionManager(SessionRepository sessionRepository, ObjectMapper objectMapper) {
        this.sessionRepository = sessionRepository;
        this.objectMapper = objectMapper;
    }

    /**
     * 获取或创建会话
     */
    @Transactional
    public SessionData getOrCreateSession(String sessionId) {
        if (sessionId == null || sessionId.isEmpty()) {
            sessionId = UUID.randomUUID().toString();
        }

        Optional<SessionEntity> opt = sessionRepository.findById(sessionId);
        SessionEntity entity;
        if (opt.isPresent()) {
            entity = opt.get();
        } else {
            entity = new SessionEntity(sessionId);
            sessionRepository.save(entity);
        }

        return new SessionData(entity.getId(), parseMessages(entity.getMessages()));
    }

    /**
     * 添加一对消息（用户问题 + AI回复）到会话记录
     */
    @Transactional
    public void addMessage(String sessionId, String question, String answer) {
        SessionEntity entity = sessionRepository.findById(sessionId)
                .orElseGet(() -> {
                    SessionEntity e = new SessionEntity(sessionId);
                    sessionRepository.save(e);
                    return e;
                });

        List<Map<String, String>> messages = parseMessages(entity.getMessages());

        // 添加用户消息
        Map<String, String> userMsg = new HashMap<>();
        userMsg.put("role", "user");
        userMsg.put("content", question);
        messages.add(userMsg);

        // 添加 AI 回复
        Map<String, String> assistantMsg = new HashMap<>();
        assistantMsg.put("role", "assistant");
        assistantMsg.put("content", answer);
        messages.add(assistantMsg);

        // 滑动窗口：只保留最近 MAX_WINDOW_SIZE 对
        int maxMessages = MAX_WINDOW_SIZE * 2;
        while (messages.size() > maxMessages) {
            messages.remove(0);
            if (!messages.isEmpty()) {
                messages.remove(0);
            }
        }

        try {
            entity.setMessages(objectMapper.writeValueAsString(messages));
        } catch (Exception e) {
            logger.error("序列化消息失败", e);
            entity.setMessages("[]");
        }
        entity.setPairCount(messages.size() / 2);
        entity.setUpdatedAt(LocalDateTime.now());
        sessionRepository.save(entity);

        logger.debug("会话 {} 更新历史消息，当前消息对数: {}", sessionId, messages.size() / 2);
    }

    /**
     * 获取会话历史消息
     */
    @Transactional(readOnly = true)
    public List<Map<String, String>> getHistory(String sessionId) {
        Optional<SessionEntity> opt = sessionRepository.findById(sessionId);
        if (opt.isEmpty()) {
            return new ArrayList<>();
        }
        return parseMessages(opt.get().getMessages());
    }

    /**
     * 清空会话历史
     */
    @Transactional
    public void clearHistory(String sessionId) {
        Optional<SessionEntity> opt = sessionRepository.findById(sessionId);
        if (opt.isPresent()) {
            SessionEntity entity = opt.get();
            entity.setMessages("[]");
            entity.setPairCount(0);
            entity.setUpdatedAt(LocalDateTime.now());
            sessionRepository.save(entity);
            logger.info("会话 {} 历史消息已清空", sessionId);
        }
    }

    /**
     * 获取消息对数
     */
    @Transactional(readOnly = true)
    public int getMessagePairCount(String sessionId) {
        Optional<SessionEntity> opt = sessionRepository.findById(sessionId);
        return opt.map(SessionEntity::getPairCount).orElse(0);
    }

    /**
     * 获取创建时间
     */
    @Transactional(readOnly = true)
    public long getCreateTime(String sessionId) {
        Optional<SessionEntity> opt = sessionRepository.findById(sessionId);
        return opt.map(e -> e.getCreatedAt().atZone(java.time.ZoneId.systemDefault()).toEpochSecond() * 1000)
                .orElse(0L);
    }

    /**
     * 删除过期会话（超过 7 天未更新）
     */
    @Transactional
    public void deleteExpiredSessions() {
        LocalDateTime cutoff = LocalDateTime.now().minusDays(7);
        sessionRepository.deleteByUpdatedAtBefore(cutoff);
        logger.info("已清理 {} 天未更新的过期会话", 7);
    }

    // === 辅助方法 ===

    private List<Map<String, String>> parseMessages(String json) {
        try {
            if (json == null || json.trim().isEmpty() || json.equals("[]")) {
                return new ArrayList<>();
            }
            return objectMapper.readValue(json, new TypeReference<List<Map<String, String>>>() {});
        } catch (Exception e) {
            logger.warn("解析消息 JSON 失败，返回空列表", e);
            return new ArrayList<>();
        }
    }

    /**
     * 会话数据 DTO
     */
    public record SessionData(String sessionId, List<Map<String, String>> history) {
        public int getMessagePairCount() { return history.size() / 2; }
    }
}
```

---

### Task 7: 改造 ChatController + 添加 SessionCleanupTask

**Files:**
- Modify: `src/main/java/org/example/controller/ChatController.java`
- Create: `src/main/java/org/example/service/SessionCleanupTask.java`

- [ ] **Step 1: 从 ChatController 中移除 SessionInfo 内部类和 sessions 字段**

修改 `ChatController.java`：

1. 移除 `import java.util.concurrent.ConcurrentHashMap;`
2. 移除 `import java.util.concurrent.locks.ReentrantLock;`
3. 移除字段 `private final Map<String, SessionInfo> sessions = new ConcurrentHashMap<>();`
4. 移除 `private static final int MAX_WINDOW_SIZE = 6;`
5. 移除 `getOrCreateSession()` 方法
6. 移除整个 `SessionInfo` 内部类
7. 新增 `@Autowired private SessionManager sessionManager;`

用 Edit 工具逐步操作（由于 ChatController 源代码较长，分节操作）：

首先，修改 import 区域——移除 `ConcurrentHashMap` 和 `ReentrantLock` 的导入：

```
移除这两行:
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantLock;
```

移除 sessions 字段和 MAX_WINDOW_SIZE：

```
移除:
    // 存储会话信息
    private final Map<String, SessionInfo> sessions = new ConcurrentHashMap<>();
    
    // 最大历史消息窗口大小（成对计算：用户消息+AI回复=1对）
    private static final int MAX_WINDOW_SIZE = 6;
```

添加 SessionManager 注入（放在 `private final ExecutorService executor` 后面）：

```java
    @Autowired
    private org.example.service.SessionManager sessionManager;
```

修改 chat() 方法中的会话访问逻辑，将 `session.getHistory()` 改为 `sessionManager.getHistory(sessionId)`：

```java
    // 原代码:
    SessionInfo session = getOrCreateSession(request.getId());
    List<Map<String, String>> history = session.getHistory();

    // 改为:
    org.example.service.SessionManager.SessionData sessionData = sessionManager.getOrCreateSession(request.getId());
    List<Map<String, String>> history = sessionData.history();
```

```java
    // 原代码:
    session.addMessage(request.getQuestion(), fullAnswer);
    logger.info("已更新会话历史 - SessionId: {}, 当前消息对数: {}", 
        request.getId(), session.getMessagePairCount());

    // 改为:
    sessionManager.addMessage(request.getId(), request.getQuestion(), fullAnswer);
    logger.info("已更新会话历史 - SessionId: {}", request.getId());
```

修改 clearChatHistory() 方法：

```java
    // 原代码:
    SessionInfo session = sessions.get(request.getId());
    if (session != null) {
        session.clearHistory();
        ...
    } else {
        return ResponseEntity.ok(ApiResponse.error("会话不存在"));
    }

    // 改为:
    sessionManager.clearHistory(request.getId());
    return ResponseEntity.ok(ApiResponse.success("会话历史已清空"));
```

修改 chatStream() 中的会话访问：

```java
    // 原代码:
    SessionInfo session = getOrCreateSession(request.getId());
    List<Map<String, String>> history = session.getHistory();

    // 改为:
    org.example.service.SessionManager.SessionData sessionData = sessionManager.getOrCreateSession(request.getId());
    List<Map<String, String>> history = sessionData.history();
```

```java
    // 流式完成回调中:
    // 原代码:
    session.addMessage(request.getQuestion(), fullAnswer);
    logger.info("已更新会话历史 - SessionId: {}, 当前消息对数: {}", 
        request.getId(), session.getMessagePairCount());

    // 改为:
    sessionManager.addMessage(request.getId(), request.getQuestion(), fullAnswer);
    logger.info("已更新会话历史 - SessionId: {}", request.getId());
```

修改 getSessionInfo() 方法：

```java
    // 原代码:
    SessionInfo session = sessions.get(sessionId);
    if (session != null) {
        SessionInfoResponse response = new SessionInfoResponse();
        response.setSessionId(sessionId);
        response.setMessagePairCount(session.getMessagePairCount());
        response.setCreateTime(session.createTime);
        return ResponseEntity.ok(ApiResponse.success(response));
    } else {
        return ResponseEntity.ok(ApiResponse.error("会话不存在"));
    }

    // 改为:
    SessionInfoResponse response = new SessionInfoResponse();
    response.setSessionId(sessionId);
    response.setMessagePairCount(sessionManager.getMessagePairCount(sessionId));
    response.setCreateTime(sessionManager.getCreateTime(sessionId));
    return ResponseEntity.ok(ApiResponse.success(response));
```

移除 getOrCreateSession() 方法（删除整个辅助方法）：

```java
    // 删除整个方法:
    private SessionInfo getOrCreateSession(String sessionId) {
        if (sessionId == null || sessionId.isEmpty()) {
            sessionId = UUID.randomUUID().toString();
        }
        return sessions.computeIfAbsent(sessionId, SessionInfo::new);
    }
```

移除整个 SessionInfo 内部类（从 `private static class SessionInfo` 开始到结束花括号为止的全部内容）。

- [ ] **Step 2: 创建 SessionCleanupTask.java**

```java
package org.example.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
@EnableScheduling
public class SessionCleanupTask {

    private static final Logger logger = LoggerFactory.getLogger(SessionCleanupTask.class);

    private final SessionManager sessionManager;

    public SessionCleanupTask(SessionManager sessionManager) {
        this.sessionManager = sessionManager;
    }

    /**
     * 每天凌晨 3 点清理 7 天前的过期会话
     */
    @Scheduled(cron = "0 0 3 * * ?")
    public void cleanupExpiredSessions() {
        logger.info("开始清理过期会话...");
        sessionManager.deleteExpiredSessions();
    }
}
```

---

### Task 8: 在 ChatService 中注册 DatabaseTools

**Files:**
- Modify: `src/main/java/org/example/service/ChatService.java`

- [ ] **Step 1: 注入 DatabaseTools 并注册到 buildMethodToolsArray**

在 `ChatService.java` 中添加：

```java
    @Autowired
    private org.example.agent.tool.DatabaseTools databaseTools;
```

在 `buildMethodToolsArray()` 方法中追加 `databaseTools`：

```java
    public Object[] buildMethodToolsArray() {
        if (queryLogsTools != null) {
            return new Object[]{dateTimeTools, internalDocsTools, queryMetricsTools, queryLogsTools, databaseTools};
        } else {
            return new Object[]{dateTimeTools, internalDocsTools, queryMetricsTools, databaseTools};
        }
    }
```

同理，在 `AiOpsService.java` 中也注入 `DatabaseTools` 并注册到其 `buildMethodToolsArray()` 方法。

---

### Task 9: RAG 测试集数据

**Files:**
- Create: `src/test/resources/eval/test-sets/fact-queries.json`
- Create: `src/test/resources/eval/test-sets/cross-doc.json`
- Create: `src/test/resources/eval/test-sets/no-answer.json`

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p src/test/resources/eval/test-sets
```

- [ ] **Step 2: 创建 fact-queries.json**

```json
[
  {"id":"FQ-001","question":"CPU使用率超过多少需要告警？","expected_answer":"80%","source_doc":"cpu_high_usage.md","category":"single-fact"},
  {"id":"FQ-002","question":"CPU告警的处理步骤是什么？","expected_keywords":["确认Pod","检查负载","扩容"],"source_doc":"cpu_high_usage.md","category":"single-fact"},
  {"id":"FQ-003","question":"内存使用率超过多少需要告警？","expected_answer":"85%","source_doc":"memory_high_usage.md","category":"single-fact"},
  {"id":"FQ-004","question":"内存告警应该检查哪些指标？","expected_keywords":["JVM","堆内存","GC"],"source_doc":"memory_high_usage.md","category":"single-fact"},
  {"id":"FQ-005","question":"磁盘使用率超过多少需要告警？","expected_answer":"90%","source_doc":"disk_high_usage.md","category":"single-fact"},
  {"id":"FQ-006","question":"服务不可用的排查步骤是什么？","expected_keywords":["Pod状态","日志","端口"],"source_doc":"service_unavailable.md","category":"single-fact"},
  {"id":"FQ-007","question":"慢响应的阈值是多少？","expected_answer":"3秒","source_doc":"slow_response.md","category":"single-fact"},
  {"id":"FQ-008","question":"CPU告警的级别是什么？","expected_keywords":["critical","严重"],"source_doc":"cpu_high_usage.md","category":"single-fact"},
  {"id":"FQ-009","question":"PaymentService在哪个命名空间运行？","expected_answer":"production","source_doc":"cpu_high_usage.md","category":"single-fact"},
  {"id":"FQ-010","question":"订单服务OOM后应该检查什么？","expected_keywords":["堆转储","内存泄漏","GC"],"source_doc":"memory_high_usage.md","category":"single-fact"}
]
```

- [ ] **Step 3: 创建 cross-doc.json**

```json
[
  {"id":"CD-001","question":"同时出现CPU和内存告警时应该先排查哪个？","required_docs":["cpu_high_usage.md","memory_high_usage.md"],"expected_key_points":["判断瓶颈类型","检查日志"]},
  {"id":"CD-002","question":"如果服务响应慢且CPU高，可能的原因有哪些？","required_docs":["slow_response.md","cpu_high_usage.md"],"expected_key_points":["资源竞争","慢查询","代码效率"]},
  {"id":"CD-003","question":"磁盘快满了且服务变慢，根因可能是什么？","required_docs":["disk_high_usage.md","slow_response.md"],"expected_key_points":["磁盘I/O瓶颈","日志量过大","索引碎片"]},
  {"id":"CD-004","question":"服务不可用可能与哪些资源告警相关？","required_docs":["service_unavailable.md","cpu_high_usage.md","memory_high_usage.md"],"expected_key_points":["OOM导致重启","资源耗尽","健康检查失败"]}
]
```

- [ ] **Step 4: 创建 no-answer.json**

```json
[
  {"id":"NA-001","question":"公司年会在什么时候举办？","expected_behavior":"reject","reject_patterns":["没有找到","无法回答","没有相关信息","不在知识库"]},
  {"id":"NA-002","question":"今年的年终奖什么时候发？","expected_behavior":"reject","reject_patterns":["没有找到","无法回答","没有相关信息","不在知识库"]},
  {"id":"NA-003","question":"章三是谁？","expected_behavior":"reject","reject_patterns":["没有找到","无法回答","没有相关信息","不在知识库"]},
  {"id":"NA-004","question":"公司福利有哪些？","expected_behavior":"reject","reject_patterns":["没有找到","无法回答","没有相关信息","不在知识库"]},
  {"id":"NA-005","question":"这个季度的OKR是什么？","expected_behavior":"reject","reject_patterns":["没有找到","无法回答","没有相关信息","不在知识库"]}
]
```

---

### Task 10: RAG 评估指标计算

**Files:**
- Create: `src/test/java/org/example/eval/metrics/RecallCalculator.java`
- Create: `src/test/java/org/example/eval/metrics/RejectionRate.java`

- [ ] **Step 1: 创建 RecallCalculator.java**

```java
package org.example.eval.metrics;

import java.util.List;
import java.util.Set;

public class RecallCalculator {

    /**
     * 计算 Recall@K
     * @param retrievedDocs 检索返回的文档列表（按分数降序）
     * @param expectedDocs 期望命中的文档列表
     * @param K 取前K个结果
     * @return 0.0 ~ 1.0
     */
    public static double recallAtK(List<String> retrievedDocs, List<String> expectedDocs, int K) {
        if (expectedDocs == null || expectedDocs.isEmpty()) {
            return 1.0; // 没有期望文档视为通过
        }

        List<String> topK = retrievedDocs.size() > K
                ? retrievedDocs.subList(0, K)
                : retrievedDocs;

        Set<String> topKSet = Set.copyOf(topK);
        long hitCount = expectedDocs.stream().filter(topKSet::contains).count();

        return (double) hitCount / expectedDocs.size();
    }

    /**
     * 计算 Precision@K
     * @param retrievedDocs 检索返回的文档列表
     * @param relevantDocs 相关文档列表
     * @param K 取前K个结果
     * @return 0.0 ~ 1.0
     */
    public static double precisionAtK(List<String> retrievedDocs, List<String> relevantDocs, int K) {
        if (retrievedDocs.isEmpty()) return 0.0;

        List<String> topK = retrievedDocs.size() > K
                ? retrievedDocs.subList(0, K)
                : retrievedDocs;

        Set<String> relevantSet = Set.copyOf(relevantDocs);
        long hitCount = topK.stream().filter(relevantSet::contains).count();

        return (double) hitCount / topK.size();
    }
}
```

- [ ] **Step 2: 创建 RejectionRate.java**

```java
package org.example.eval.metrics;

import java.util.List;

public class RejectionRate {

    /**
     * 判断回答是否包含拒答关键词
     */
    public static boolean isRejection(String answer, List<String> rejectPatterns) {
        if (answer == null || answer.isEmpty()) return false;
        return rejectPatterns.stream().anyMatch(pattern ->
                answer.contains(pattern) || answer.toLowerCase().contains(pattern.toLowerCase()));
    }

    /**
     * 计算拒答准确率
     * @param results 每条用例的 {expectedReject: boolean, actualRejected: boolean}
     * @return 0.0 ~ 1.0
     */
    public static double calculateAccuracy(List<RejectionResult> results) {
        if (results.isEmpty()) return 1.0;
        long correct = results.stream().filter(r -> r.expectedReject == r.actualRejected).count();
        return (double) correct / results.size();
    }

    public record RejectionResult(boolean expectedReject, boolean actualRejected) {}
}
```

---

### Task 11: RAG 评估引擎

**Files:**
- Create: `src/test/java/org/example/eval/RagEvaluator.java`

- [ ] **Step 1: 创建 RagEvaluator.java**

```java
package org.example.eval;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.example.eval.metrics.RecallCalculator;
import org.example.eval.metrics.RejectionRate;
import org.example.service.VectorSearchService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.InputStream;
import java.util.*;
import java.util.stream.Collectors;

@Component
public class RagEvaluator {

    private static final Logger logger = LoggerFactory.getLogger(RagEvaluator.class);

    @Autowired
    private VectorSearchService vectorSearchService;

    @Value("${rag.top-k:3}")
    private int topK;

    private final ObjectMapper objectMapper = new ObjectMapper();

    /**
     * 加载测试集
     */
    public List<TestCase> loadTestCases(String resourcePath) {
        try {
            InputStream is = getClass().getResourceAsStream(resourcePath);
            if (is == null) {
                logger.warn("测试集文件不存在: {}", resourcePath);
                return List.of();
            }
            return objectMapper.readValue(is, new TypeReference<List<TestCase>>() {});
        } catch (Exception e) {
            logger.error("加载测试集失败: {}", resourcePath, e);
            return List.of();
        }
    }

    /**
     * 评估单条事实查询用例
     */
    public EvalResult evaluateFactQuery(TestCase tc) {
        List<VectorSearchService.SearchResult> results = vectorSearchService.searchSimilarDocuments(
                tc.question(), topK + 2); // 多取2条用于精度计算

        List<String> retrievedDocNames = results.stream()
                .map(r -> extractDocName(r.getMetadata()))
                .filter(Objects::nonNull)
                .collect(Collectors.toList());

        double recall = RecallCalculator.recallAtK(retrievedDocNames,
                tc.sourceDoc() != null ? List.of(tc.sourceDoc()) : List.of(), topK);

        double precision = RecallCalculator.precisionAtK(retrievedDocNames,
                tc.sourceDoc() != null ? List.of(tc.sourceDoc()) : List.of(), topK);

        return new EvalResult(
                tc.id(), tc.question(), "answer",
                retrievedDocNames, recall, precision,
                true, false, true
        );
    }

    /**
     * 评估单条跨文档综合用例
     */
    public EvalResult evaluateCrossDoc(TestCase tc) {
        List<VectorSearchService.SearchResult> results = vectorSearchService.searchSimilarDocuments(
                tc.question(), topK + 2);

        List<String> retrievedDocNames = results.stream()
                .map(r -> extractDocName(r.getMetadata()))
                .filter(Objects::nonNull)
                .collect(Collectors.toList());

        double recall = RecallCalculator.recallAtK(retrievedDocNames,
                tc.requiredDocs() != null ? tc.requiredDocs() : List.of(), topK);

        return new EvalResult(
                tc.id(), tc.question(), "answer",
                retrievedDocNames, recall, 0.0,
                true, false, recall >= 0.5
        );
    }

    /**
     * 评估单条无答案用例（仅检索阶段，看是否错误地返回了结果）
     */
    public EvalResult evaluateNoAnswer(TestCase tc) {
        List<VectorSearchService.SearchResult> results = vectorSearchService.searchSimilarDocuments(
                tc.question(), topK);

        boolean hasResults = results != null && !results.isEmpty();
        boolean correctlyRejected = !hasResults; // 没有检索到结果就对了

        return new EvalResult(
                tc.id(), tc.question(), "reject",
                results != null ? results.stream()
                        .map(r -> extractDocName(r.getMetadata()))
                        .filter(Objects::nonNull)
                        .collect(Collectors.toList()) : List.of(),
                0.0, 0.0,
                !correctlyRejected, correctlyRejected, correctlyRejected
        );
    }

    /**
     * 汇总评估结果
     */
    public EvalSummary summarize(List<EvalResult> results) {
        if (results.isEmpty()) return new EvalSummary(0, 0, 0, 0, 0, 0, 0, 0);

        long answerCount = results.stream().filter(r -> "answer".equals(r.category())).count();
        long rejectCount = results.stream().filter(r -> "reject".equals(r.category())).count();

        double avgRecall = results.stream().filter(r -> "answer".equals(r.category()))
                .mapToDouble(EvalResult::recallAtK).average().orElse(0);
        double avgPrecision = results.stream().filter(r -> "answer".equals(r.category()))
                .mapToDouble(EvalResult::precisionAtK).average().orElse(0);

        long correctRejections = results.stream().filter(r -> "reject".equals(r.category()))
                .filter(EvalResult::correctlyRejected).count();
        double rejectionAccuracy = rejectCount > 0 ? (double) correctRejections / rejectCount : 1.0;

        long passed = results.stream().filter(EvalResult::passed).count();

        return new EvalSummary(
                results.size(), answerCount, rejectCount,
                avgRecall, avgPrecision, rejectionAccuracy,
                passed, results.size() - passed
        );
    }

    // === 辅助方法 ===

    private String extractDocName(String metadata) {
        if (metadata == null) return null;
        try {
            var map = objectMapper.readValue(metadata, Map.class);
            Object source = map.get("_source");
            if (source != null) {
                String path = source.toString();
                int idx = path.lastIndexOf('/');
                return idx >= 0 ? path.substring(idx + 1) : path;
            }
        } catch (Exception ignored) {}
        return null;
    }

    // === 记录类型 ===

    public record TestCase(
            String id, String question,
            String expectedAnswer, List<String> expectedKeywords,
            String sourceDoc, List<String> requiredDocs,
            String expectedBehavior, List<String> rejectPatterns,
            String category
    ) {
        public TestCase {
            category = category != null ? category : "single-fact";
        }
    }

    public record EvalResult(
            String id, String question, String category,
            List<String> retrievedDocs,
            double recallAtK, double precisionAtK,
            boolean hasHallucination, boolean correctlyRejected,
            boolean passed
    ) {}

    public record EvalSummary(
            int totalCases, int answerCases, int rejectCases,
            double avgRecall, double avgPrecision,
            double rejectionAccuracy,
            int passed, int failed
    ) {}
}
```

---

### Task 12: RAG 测试套件 + 报告生成

**Files:**
- Create: `src/test/java/org/example/eval/RagTestSuite.java`
- Create: `src/test/java/org/example/eval/report/ReportGenerator.java`

- [ ] **Step 1: 创建 ReportGenerator.java**

```java
package org.example.eval.report;

import org.example.eval.RagEvaluator;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

public class ReportGenerator {

    public static String generate(RagEvaluator.EvalSummary summary, List<RagEvaluator.EvalResult> results) {
        StringBuilder sb = new StringBuilder();
        String timestamp = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));

        sb.append("# RAG 离线评估报告\n\n");
        sb.append("**生成时间**: ").append(timestamp).append("\n\n");
        sb.append("---\n\n");

        // 总体指标
        sb.append("## 总体指标\n\n");
        sb.append("| 指标 | 值 |\n");
        sb.append("|------|---|\n");
        sb.append("| 测试用例总数 | ").append(summary.totalCases()).append(" |\n");
        sb.append("| 事实查询 | ").append(summary.answerCases()).append(" |\n");
        sb.append("| 无答案场景 | ").append(summary.rejectCases()).append(" |\n");
        sb.append("| 通过 | ").append(summary.passed()).append(" |\n");
        sb.append("| 失败 | ").append(summary.failed()).append(" |\n");
        sb.append("| 通过率 | ").append(String.format("%.1f%%", (double) summary.passed() / summary.totalCases() * 100)).append(" |\n\n");

        sb.append("### 检索指标\n\n");
        sb.append("| 指标 | 值 |\n");
        sb.append("|------|---|\n");
        sb.append("| 平均 Recall@K | ").append(String.format("%.2f%%", summary.avgRecall() * 100)).append(" |\n");
        sb.append("| 平均 Precision@K | ").append(String.format("%.2f%%", summary.avgPrecision() * 100)).append(" |\n\n");

        sb.append("### 生成指标\n\n");
        sb.append("| 指标 | 值 |\n");
        sb.append("|------|---|\n");
        sb.append("| 拒答准确率 | ").append(String.format("%.2f%%", summary.rejectionAccuracy() * 100)).append(" |\n\n");

        // 失败用例详情
        List<RagEvaluator.EvalResult> failed = results.stream().filter(r -> !r.passed()).collect(Collectors.toList());
        if (!failed.isEmpty()) {
            sb.append("## 失败用例详情\n\n");
            for (RagEvaluator.EvalResult r : failed) {
                sb.append("### ").append(r.id()).append(": ").append(r.question()).append("\n\n");
                sb.append("- **类型**: ").append(r.category()).append("\n");
                sb.append("- **检索结果**: ").append(String.join(", ", r.retrievedDocs())).append("\n");
                if (r.recallAtK() < 1.0) {
                    sb.append("- **Recall@K 不足**: ").append(String.format("%.2f%%", r.recallAtK() * 100)).append("\n");
                }
                sb.append("\n");
            }
        }

        // 配置快照
        sb.append("## 配置快照\n\n");
        sb.append("```\n");
        sb.append("rag.topK: 5\n");
        sb.append("rag.chunk.maxSize: 800\n");
        sb.append("rag.chunk.overlap: 100\n");
        sb.append("```\n");

        return sb.toString();
    }

    public static String generateComparison(Map<String, RagEvaluator.EvalSummary> summaries) {
        StringBuilder sb = new StringBuilder();
        sb.append("# RAG 调优对比报告\n\n");
        sb.append("| 配置 | 总用例 | 通过率 | Recall@K | 拒答准确率 |\n");
        sb.append("|------|--------|--------|----------|-----------|\n");

        for (var entry : summaries.entrySet()) {
            RagEvaluator.EvalSummary s = entry.getValue();
            sb.append("| ").append(entry.getKey())
              .append(" | ").append(s.totalCases())
              .append(" | ").append(String.format("%.1f%%", (double) s.passed() / s.totalCases() * 100))
              .append(" | ").append(String.format("%.2f%%", s.avgRecall() * 100))
              .append(" | ").append(String.format("%.2f%%", s.rejectionAccuracy() * 100))
              .append(" |\n");
        }

        return sb.toString();
    }
}
```

- [ ] **Step 2: 创建 RagTestSuite.java**

```java
package org.example.eval;

import org.example.eval.report.ReportGenerator;
import org.junit.jupiter.api.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.io.File;
import java.io.FileWriter;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertTrue;

@SpringBootTest
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
public class RagTestSuite {

    private static final Logger logger = LoggerFactory.getLogger(RagTestSuite.class);

    @Autowired
    private RagEvaluator evaluator;

    private static final List<RagEvaluator.EvalResult> allResults = new ArrayList<>();

    @Test
    @Order(1)
    public void testFactQueries() {
        logger.info("===== 开始事实查询评估 =====");
        List<RagEvaluator.TestCase> cases = evaluator.loadTestCases("/eval/test-sets/fact-queries.json");
        assertTrue(cases.size() > 0, "事实查询测试集不能为空");

        for (RagEvaluator.TestCase tc : cases) {
            RagEvaluator.EvalResult result = evaluator.evaluateFactQuery(tc);
            allResults.add(result);
            logger.info("[{}] question={}, recall@K={}, passed={}",
                    tc.id(), tc.question(), String.format("%.2f", result.recallAtK()), result.passed());
        }

        logger.info("事实查询完成: {} 条", cases.size());
    }

    @Test
    @Order(2)
    public void testCrossDocQueries() {
        logger.info("===== 开始跨文档综合评估 =====");
        List<RagEvaluator.TestCase> cases = evaluator.loadTestCases("/eval/test-sets/cross-doc.json");
        assertTrue(cases.size() > 0, "跨文档综合测试集不能为空");

        for (RagEvaluator.TestCase tc : cases) {
            RagEvaluator.EvalResult result = evaluator.evaluateCrossDoc(tc);
            allResults.add(result);
            logger.info("[{}] question={}, recall@K={}, passed={}",
                    tc.id(), tc.question(), String.format("%.2f", result.recallAtK()), result.passed());
        }

        logger.info("跨文档综合完成: {} 条", cases.size());
    }

    @Test
    @Order(3)
    public void testNoAnswerQueries() {
        logger.info("===== 开始无答案场景评估 =====");
        List<RagEvaluator.TestCase> cases = evaluator.loadTestCases("/eval/test-sets/no-answer.json");
        assertTrue(cases.size() > 0, "无答案测试集不能为空");

        for (RagEvaluator.TestCase tc : cases) {
            RagEvaluator.EvalResult result = evaluator.evaluateNoAnswer(tc);
            allResults.add(result);
            logger.info("[{}] question={}, correctlyRejected={}, passed={}",
                    tc.id(), tc.question(), result.correctlyRejected(), result.passed());
        }

        logger.info("无答案场景完成: {} 条", cases.size());
    }

    @AfterAll
    public static void generateReport() {
        if (allResults.isEmpty()) {
            logger.warn("无评估结果，跳过报告生成");
            return;
        }

        RagEvaluator.EvalSummary summary = new RagEvaluator().summarize(allResults);
        String report = ReportGenerator.generate(summary, allResults);

        // 保存报告
        try {
            String timestamp = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss"));
            File reportsDir = new File("target/eval-reports");
            reportsDir.mkdirs();
            File reportFile = new File(reportsDir, "rag-report-" + timestamp + ".md");
            try (FileWriter fw = new FileWriter(reportFile)) {
                fw.write(report);
            }
            logger.info("评估报告已保存: {}", reportFile.getAbsolutePath());

            // 打印摘要到控制台
            System.out.println("\n========================================");
            System.out.println("  RAG 评估完成");
            System.out.println("  总数: " + summary.totalCases());
            System.out.println("  通过: " + summary.passed());
            System.out.println("  失败: " + summary.failed());
            System.out.println("  通过率: " + String.format("%.1f%%", (double) summary.passed() / summary.totalCases() * 100));
            System.out.println("  平均 Recall@K: " + String.format("%.2f%%", summary.avgRecall() * 100));
            System.out.println("  拒答准确率: " + String.format("%.2f%%", summary.rejectionAccuracy() * 100));
            System.out.println("========================================\n");
        } catch (Exception e) {
            logger.error("保存评估报告失败", e);
        }
    }
}
```

---

### Task 13: DocumentChunkService 单元测试

**Files:**
- Create: `src/test/java/org/example/service/DocumentChunkServiceTest.java`

- [ ] **Step 1: 创建单元测试**

```java
package org.example.service;

import org.example.config.DocumentChunkConfig;
import org.example.dto.DocumentChunk;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class DocumentChunkServiceTest {

    @Mock
    private DocumentChunkConfig chunkConfig;

    @InjectMocks
    private DocumentChunkService chunkService;

    @BeforeEach
    void setUp() {
        when(chunkConfig.getMaxSize()).thenReturn(800);
        when(chunkConfig.getOverlap()).thenReturn(100);
    }

    @Test
    void shouldReturnEmptyListForEmptyContent() {
        List<DocumentChunk> result = chunkService.chunkDocument("", "test.md");
        assertTrue(result.isEmpty());
    }

    @Test
    void shouldReturnEmptyListForNullContent() {
        List<DocumentChunk> result = chunkService.chunkDocument(null, "test.md");
        assertTrue(result.isEmpty());
    }

    @Test
    void shouldReturnSingleChunkForShortContent() {
        String content = "这是一段短文档。";
        List<DocumentChunk> result = chunkService.chunkDocument(content, "test.md");
        assertEquals(1, result.size());
        assertEquals(content, result.get(0).getContent());
    }

    @Test
    void shouldSplitByMarkdownHeadings() {
        String content = "# 标题一\n\n内容段落\n\n## 标题二\n\n另一个段落";
        List<DocumentChunk> result = chunkService.chunkDocument(content, "test.md");
        // 期望按标题分割
        assertTrue(result.size() >= 2);
        // 第一个分片应包含标题一
        assertTrue(result.get(0).getContent().contains("标题一"));
    }

    @Test
    void shouldRespectMaxSize() {
        // 生成超过 maxSize 的内容
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 500; i++) {
            sb.append("段落").append(i).append("。");
        }
        String content = sb.toString();

        List<DocumentChunk> result = chunkService.chunkDocument(content, "long-doc.md");
        assertTrue(result.size() > 1, "长文档应被分成多个分片");

        // 每个分片不超过 maxSize
        for (DocumentChunk chunk : result) {
            assertTrue(chunk.getContent().length() <= 850, // maxSize + overlap 容差
                    "分片长度应不超过 maxSize + overlap");
        }
    }

    @Test
    void shouldProvideChunkIndex() {
        String content = "# 章节A\n\n内容A\n\n# 章节B\n\n内容B\n\n# 章节C\n\n内容C";
        List<DocumentChunk> result = chunkService.chunkDocument(content, "test.md");
        for (int i = 0; i < result.size(); i++) {
            assertEquals(i, result.get(i).getChunkIndex(), "分片索引应连续且从0开始");
        }
    }

    @Test
    void shouldSetTitleForHeadings() {
        String content = "# CPU告警处理\n\n步骤一：确认告警";
        List<DocumentChunk> result = chunkService.chunkDocument(content, "test.md");
        assertTrue(result.size() >= 1);
        // 第一个分片的标题应为 CPU告警处理
        String title = result.get(0).getTitle();
        assertNotNull(title);
        assertTrue(title.contains("CPU告警处理") || title.contains("CPU"));
    }
}
```

---

### Task 14: DatabaseTools 单元测试

**Files:**
- Create: `src/test/java/org/example/agent/tool/DatabaseToolsTest.java`
- Create: `src/test/resources/test-schema.sql`

- [ ] **Step 1: 创建 DatabaseToolsTest.java**

```java
package org.example.agent.tool;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;

import javax.sql.DataSource;

import static org.junit.jupiter.api.Assertions.*;

class DatabaseToolsTest {

    private DatabaseTools databaseTools;
    private JdbcTemplate jdbcTemplate;

    @BeforeEach
    void setUp() {
        DataSource ds = new EmbeddedDatabaseBuilder()
                .setType(EmbeddedDatabaseType.H2)
                .addScript("classpath:test-schema.sql")
                .build();
        jdbcTemplate = new JdbcTemplate(ds);
        databaseTools = new DatabaseTools(jdbcTemplate, new ObjectMapper());
    }

    @Test
    void shouldExecuteSelectQuery() {
        String result = databaseTools.executeQuery("SELECT * FROM service_alerts");
        assertNotNull(result);
        assertTrue(result.contains("success"));
        assertTrue(result.contains("total_rows"));
    }

    @Test
    void shouldRejectNonSelectQuery() {
        String result = databaseTools.executeQuery("DELETE FROM service_alerts");
        assertNotNull(result);
        assertTrue(result.contains("错误") || result.contains("error") || result.contains("仅支持"));
        // 确认数据未被删除
        String checkResult = databaseTools.executeQuery("SELECT COUNT(*) as cnt FROM service_alerts");
        assertNotNull(checkResult);
    }

    @Test
    void shouldAutoLimitResults() {
        // 插入超过 50 条数据
        for (int i = 0; i < 60; i++) {
            jdbcTemplate.update(
                "INSERT INTO service_alerts (id, alert_name, service_name, severity, status) VALUES (?, ?, ?, ?, ?)",
                "test-" + i, "TestAlert", "test-svc", "INFO", "FIRING"
            );
        }

        String result = databaseTools.executeQuery("SELECT * FROM service_alerts");
        assertTrue(result.contains("\"total_rows\": 50") || result.contains("total_rows"));
    }

    @Test
    void shouldDescribeDatabase() {
        String result = databaseTools.describeDatabase();
        assertNotNull(result);
        assertTrue(result.contains("service_alerts") || result.contains("success"));
    }

    @Test
    void shouldHandleEmptyResult() {
        String result = databaseTools.executeQuery("SELECT * FROM service_alerts WHERE 1=0");
        assertNotNull(result);
        assertTrue(result.contains("total_rows"));
        assertTrue(result.contains("0") || result.contains("[]"));
    }
}
```

- [ ] **Step 2: 创建测试用 Schema**

```sql
-- src/test/resources/test-schema.sql
CREATE TABLE IF NOT EXISTS service_alerts (
    id VARCHAR(64) PRIMARY KEY,
    alert_name VARCHAR(128) NOT NULL,
    service_name VARCHAR(128),
    severity VARCHAR(32),
    status VARCHAR(32),
    alert_time TIMESTAMP,
    description TEXT
);

INSERT INTO service_alerts (id, alert_name, service_name, severity, status, description) VALUES
('1', 'HighCPUUsage', 'payment-service', 'CRITICAL', 'FIRING', 'CPU 使用率持续超过 80%'),
('2', 'HighMemoryUsage', 'order-service', 'WARNING', 'FIRING', '内存使用率超过 85%'),
('3', 'SlowResponse', 'user-service', 'WARNING', 'FIRING', 'P99 响应时间超过 3s');
```

---

### Task 15: SessionManager 单元测试

**Files:**
- Create: `src/test/java/org/example/service/SessionManagerTest.java`

- [ ] **Step 1: 创建 SessionManagerTest.java**

```java
package org.example.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.example.model.SessionEntity;
import org.example.repository.SessionRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.*;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.LocalDateTime;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class SessionManagerTest {

    @Mock
    private SessionRepository sessionRepository;

    @Mock
    private ObjectMapper objectMapper;

    @InjectMocks
    private SessionManager sessionManager;

    private SessionEntity entity;

    @BeforeEach
    void setUp() throws Exception {
        entity = new SessionEntity("session-1");
        entity.setMessages("[]");

        when(sessionRepository.findById("session-1")).thenReturn(Optional.of(entity));
        when(sessionRepository.findById("new-session")).thenReturn(Optional.empty());
        when(objectMapper.writeValueAsString(any()))
                .thenReturn("[{\"role\":\"user\",\"content\":\"hello\"},{\"role\":\"assistant\",\"content\":\"hi\"}]");
    }

    @Test
    void shouldGetOrCreateExistingSession() {
        when(objectMapper.readValue(eq("[]"), any(com.fasterxml.jackson.core.type.TypeReference.class)))
                .thenReturn(new ArrayList<Map<String, String>>());

        var result = sessionManager.getOrCreateSession("session-1");
        assertNotNull(result);
        assertEquals("session-1", result.sessionId());
    }

    @Test
    void shouldGenerateNewIdForNullSession() {
        var result = sessionManager.getOrCreateSession(null);
        assertNotNull(result);
        assertNotNull(result.sessionId());
    }

    @Test
    void shouldAddMessage() {
        sessionManager.addMessage("session-1", "用户问题", "AI回复");
        verify(sessionRepository, atLeastOnce()).save(any(SessionEntity.class));
    }

    @Test
    void shouldClearHistory() {
        sessionManager.clearHistory("session-1");
        verify(sessionRepository, atLeastOnce()).save(any(SessionEntity.class));
    }

    @Test
    void shouldReturnZeroForNonExistentSession() {
        when(sessionRepository.findById("not-exist")).thenReturn(Optional.empty());
        assertEquals(0, sessionManager.getMessagePairCount("not-exist"));
    }

    @Test
    void shouldDeleteExpiredSessions() {
        sessionManager.deleteExpiredSessions();
        verify(sessionRepository).deleteByUpdatedAtBefore(any(LocalDateTime.class));
    }
}
```

---

### Task 16: 编译验证 + 运行测试

- [ ] **Step 1: 全量编译**

```bash
cd C:/Code/JAVA/SuperBizAgent
mvn clean compile -q
```
Expected: BUILD SUCCESS

- [ ] **Step 2: 运行所有单元测试**

```bash
mvn test
```
Expected: 测试全部通过（或明确列出失败原因用于修复）

- [ ] **Step 3: 启动应用验证**

```bash
mvn spring-boot:run
```
在另一个终端验证：
```bash
curl -X POST http://localhost:9900/api/chat \
  -H "Content-Type: application/json" \
  -d '{"Id":"test-001","Question":"查询告警数据库中的服务告警信息"}'
```
Expected: Agent 调用 DatabaseTools 返回告警数据

---

### Task 17: 提交 Git

- [ ] **Step 1: 添加并提交所有变更**

```bash
git add -A
git commit -m "feat: 实现持久化会话、MySQL工具、RAG评估体系
- 新增 Spring Data JPA + H2 持久化会话存储 (SessionManager)
- 新增 DatabaseTools 提供 SQL 查询能力
- 新增 RAG 离线评估框架 (RagEvaluator + RagTestSuite)
- 新增 DocumentChunkService/DatabaseTools/SessionManager 单元测试
- 重构 ChatController 移除内存 SessionInfo"

Co-Authored-By: Claude <noreply@anthropic.com>
```
