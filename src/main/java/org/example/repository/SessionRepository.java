package org.example.repository;

import org.example.model.SessionEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.LocalDateTime;

@Repository
public interface SessionRepository extends JpaRepository<SessionEntity, String> {

    void deleteByUpdatedAtBefore(LocalDateTime cutoff);
}
