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

        sb.append("## 配置快照\n\n```\n");
        sb.append("rag.topK: 5\nrag.chunk.maxSize: 800\nrag.chunk.overlap: 100\n");
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
