package org.example.eval;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.example.eval.metrics.RecallCalculator;
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

    public EvalResult evaluateFactQuery(TestCase tc) {
        List<VectorSearchService.SearchResult> results = vectorSearchService.searchSimilarDocuments(
                tc.question(), topK + 2);

        List<String> retrievedDocNames = results.stream()
                .map(r -> extractDocName(r.getMetadata()))
                .filter(Objects::nonNull)
                .collect(Collectors.toList());

        double recall = RecallCalculator.recallAtK(retrievedDocNames,
                tc.sourceDoc() != null ? List.of(tc.sourceDoc()) : List.of(), topK);
        double precision = RecallCalculator.precisionAtK(retrievedDocNames,
                tc.sourceDoc() != null ? List.of(tc.sourceDoc()) : List.of(), topK);

        boolean passed = recall >= 0.5;
        return new EvalResult(tc.id(), tc.question(), "answer",
                retrievedDocNames, recall, precision, false, false, passed);
    }

    public EvalResult evaluateCrossDoc(TestCase tc) {
        List<VectorSearchService.SearchResult> results = vectorSearchService.searchSimilarDocuments(
                tc.question(), topK + 2);

        List<String> retrievedDocNames = results.stream()
                .map(r -> extractDocName(r.getMetadata()))
                .filter(Objects::nonNull)
                .collect(Collectors.toList());

        double recall = RecallCalculator.recallAtK(retrievedDocNames,
                tc.requiredDocs() != null ? tc.requiredDocs() : List.of(), topK);

        return new EvalResult(tc.id(), tc.question(), "answer",
                retrievedDocNames, recall, 0.0, true, false, recall >= 0.5);
    }

    public EvalResult evaluateNoAnswer(TestCase tc) {
        List<VectorSearchService.SearchResult> results = vectorSearchService.searchSimilarDocuments(
                tc.question(), topK);

        boolean hasResults = results != null && !results.isEmpty();
        boolean correctlyRejected = !hasResults;

        return new EvalResult(tc.id(), tc.question(), "reject",
                results != null ? results.stream()
                        .map(r -> extractDocName(r.getMetadata()))
                        .filter(Objects::nonNull)
                        .collect(Collectors.toList()) : List.of(),
                0.0, 0.0, !correctlyRejected, correctlyRejected, correctlyRejected);
    }

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

        return new EvalSummary((int) results.size(), (int) answerCount, (int) rejectCount,
                avgRecall, avgPrecision, rejectionAccuracy, (int) passed, (int) (results.size() - passed));
    }

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
