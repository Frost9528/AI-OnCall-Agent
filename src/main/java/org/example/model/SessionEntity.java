package org.example.model;

import jakarta.persistence.*;
import java.time.LocalDateTime;

@Entity
@Table(name = "chat_session")
public class SessionEntity {

    @Id
    @Column(length = 64)
    private String id;

    @Column(columnDefinition = "TEXT", nullable = false)
    private String messages;  // JSON: [{"role":"user","content":"..."},...]

    @Column(name = "pair_count", nullable = false)
    private Integer pairCount = 0;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    public SessionEntity() {}

    public SessionEntity(String id) {
        this.id = id;
        this.messages = "[]";
        this.pairCount = 0;
        this.createdAt = LocalDateTime.now();
        this.updatedAt = LocalDateTime.now();
    }

    // Getters and Setters
    public String getId() { return id; }
    public void setId(String id) { this.id = id; }

    public String getMessages() { return messages; }
    public void setMessages(String messages) { this.messages = messages; }

    public Integer getPairCount() { return pairCount; }
    public void setPairCount(Integer pairCount) { this.pairCount = pairCount; }

    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }

    public LocalDateTime getUpdatedAt() { return updatedAt; }
    public void setUpdatedAt(LocalDateTime updatedAt) { this.updatedAt = updatedAt; }

    @PreUpdate
    public void preUpdate() {
        this.updatedAt = LocalDateTime.now();
    }
}
