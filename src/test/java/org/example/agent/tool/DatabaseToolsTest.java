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
        assertTrue(result.contains("仅支持") || result.contains("error"));
        String checkResult = databaseTools.executeQuery("SELECT * FROM service_alerts");
        assertNotNull(checkResult);
    }

    @Test
    void shouldHandleEmptyResult() {
        String result = databaseTools.executeQuery("SELECT * FROM service_alerts WHERE 1=0");
        assertNotNull(result);
        assertTrue(result.contains("total_rows"));
    }

    @Test
    void shouldDescribeDatabase() {
        String result = databaseTools.describeDatabase();
        assertNotNull(result);
        assertTrue(result.contains("success"));
    }

    @Test
    void shouldNotDoubleLimit() {
        String result = databaseTools.executeQuery("SELECT * FROM service_alerts LIMIT 10");
        assertNotNull(result);
        assertTrue(result.contains("success"));
    }

    @Test
    void shouldRejectIntoOutfile() {
        String result = databaseTools.executeQuery("SELECT * FROM service_alerts INTO OUTFILE '/tmp/evil'");
        assertNotNull(result);
        assertTrue(result.contains("仅支持") || result.contains("error"));
    }
}
