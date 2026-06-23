CREATE TABLE IF NOT EXISTS service_alerts(id VARCHAR(64) PRIMARY KEY, alert_name VARCHAR(128), service_name VARCHAR(128), severity VARCHAR(32), status VARCHAR(32));
MERGE INTO service_alerts KEY(id) VALUES('1', 'HighCPUUsage', 'payment-service', 'CRITICAL', 'FIRING');
MERGE INTO service_alerts KEY(id) VALUES('2', 'HighMemoryUsage', 'order-service', 'WARNING', 'FIRING');
MERGE INTO service_alerts KEY(id) VALUES('3', 'SlowResponse', 'user-service', 'WARNING', 'FIRING');