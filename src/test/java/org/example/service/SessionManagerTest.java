package org.example.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.example.model.SessionEntity;
import org.example.repository.SessionRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.quality.Strictness;
import org.mockito.junit.jupiter.MockitoSettings;

import java.time.LocalDateTime;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
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
    }

    @Test
    void shouldGetOrCreateExistingSession() throws Exception {
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
