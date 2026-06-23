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

        try {
            String timestamp = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss"));
            File reportsDir = new File("target/eval-reports");
            reportsDir.mkdirs();
            File reportFile = new File(reportsDir, "rag-report-" + timestamp + ".md");
            try (FileWriter fw = new FileWriter(reportFile)) {
                fw.write(report);
            }
            logger.info("评估报告已保存: {}", reportFile.getAbsolutePath());

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
