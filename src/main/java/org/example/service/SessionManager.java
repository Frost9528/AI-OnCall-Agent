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
