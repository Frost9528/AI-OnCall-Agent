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
            String trimmed = sql.trim();
            String upper = trimmed.toUpperCase();
            if (!upper.startsWith("SELECT") || upper.contains("INTO OUTFILE") || upper.contains("INTO DUMPFILE")) {
                return "{\"success\": false, \"error\": \"仅支持 SELECT 查询\"}";
            }

            // 自动加 LIMIT（如果没带），使用正则避免匹配到注释或字符串中的 'LIMIT'
            // 只匹配不在引号内且作为独立关键字的 LIMIT
            if (!upper.matches(".*\\bLIMIT\\s+\\d+\\b.*")) {
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
                String safeTableName = "`" + tableName.replace("`", "``") + "`";
                Integer count = jdbcTemplate.queryForObject(
                        "SELECT COUNT(*) FROM " + safeTableName, Integer.class);
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
