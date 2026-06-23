package org.example.service;

import org.example.config.DocumentChunkConfig;
import org.example.dto.DocumentChunk;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.quality.Strictness;
import org.mockito.junit.jupiter.MockitoSettings;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
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
        assertTrue(result.size() >= 2);
        assertTrue(result.get(0).getContent().contains("标题一"));
    }

    @Test
    void shouldRespectMaxSize() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 500; i++) {
            sb.append("段落").append(i).append("。\n\n");  // 段落之间用双换行分割
        }
        String content = sb.toString();
        List<DocumentChunk> result = chunkService.chunkDocument(content, "long-doc.md");
        assertTrue(result.size() > 1, "长文档应被分成多个分片");
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
        String title = result.get(0).getTitle();
        assertNotNull(title);
        assertTrue(title.contains("CPU告警处理") || title.contains("CPU"));
    }
}
