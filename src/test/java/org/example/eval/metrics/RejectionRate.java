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
     */
    public static double calculateAccuracy(List<RejectionResult> results) {
        if (results.isEmpty()) return 1.0;
        long correct = results.stream().filter(r -> r.expectedReject == r.actualRejected).count();
        return (double) correct / results.size();
    }

    public record RejectionResult(boolean expectedReject, boolean actualRejected) {}
}
