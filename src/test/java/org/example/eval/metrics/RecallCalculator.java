package org.example.eval.metrics;

import java.util.List;
import java.util.Set;

public class RecallCalculator {

    /**
     * 计算 Recall@K
     * @param retrievedDocs 检索返回的文档列表（按分数降序）
     * @param expectedDocs 期望命中文档列表
     * @param K 取前K个结果
     * @return 0.0 ~ 1.0
     */
    public static double recallAtK(List<String> retrievedDocs, List<String> expectedDocs, int K) {
        if (expectedDocs == null || expectedDocs.isEmpty()) {
            return 1.0;
        }
        List<String> topK = retrievedDocs.size() > K ? retrievedDocs.subList(0, K) : retrievedDocs;
        Set<String> topKSet = Set.copyOf(topK);
        long hitCount = expectedDocs.stream().filter(topKSet::contains).count();
        return (double) hitCount / expectedDocs.size();
    }

    /**
     * 计算 Precision@K
     */
    public static double precisionAtK(List<String> retrievedDocs, List<String> relevantDocs, int K) {
        if (retrievedDocs.isEmpty()) return 0.0;
        List<String> topK = retrievedDocs.size() > K ? retrievedDocs.subList(0, K) : retrievedDocs;
        Set<String> relevantSet = Set.copyOf(relevantDocs);
        long hitCount = topK.stream().filter(relevantSet::contains).count();
        return (double) hitCount / topK.size();
    }
}
