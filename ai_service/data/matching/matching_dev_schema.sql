-- 매칭 개발 DB용 사용자·지역·매칭 테이블을 정의한다. 백엔드 운영 마이그레이션과 별도로 사용한다.
CREATE TABLE regions (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL,
 full_name VARCHAR(150) NOT NULL UNIQUE, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
) ENGINE=InnoDB;
CREATE TABLE users (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, nickname VARCHAR(20) NOT NULL,
 profile_image_url VARCHAR(2048), status VARCHAR(20) NOT NULL,
 created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, deleted_at DATETIME
) ENGINE=InnoDB;
CREATE TABLE matching_profiles (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, user_id BIGINT NOT NULL UNIQUE,
 is_active BOOLEAN NOT NULL, gender VARCHAR(20), pace VARCHAR(20) NOT NULL,
 created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
 FOREIGN KEY(user_id) REFERENCES users(id),
 CHECK(gender IS NULL OR gender IN ('FEMALE','MALE')),
 CHECK(pace IN ('RELAXED','BALANCED','PACKED'))
) ENGINE=InnoDB;
CREATE TABLE matching_profile_themes (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, matching_profile_id BIGINT NOT NULL, theme VARCHAR(20) NOT NULL,
 UNIQUE(matching_profile_id,theme), FOREIGN KEY(matching_profile_id) REFERENCES matching_profiles(id),
 CHECK(theme IN ('NATURE','FOOD','CULTURE','REST','SNS','ACTIVITY'))
) ENGINE=InnoDB;
CREATE TABLE matching_requests (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, requester_id BIGINT NOT NULL,
 preferred_companion_gender VARCHAR(20) NOT NULL, pace VARCHAR(20) NOT NULL,
 budget_min INT, budget_max INT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
 FOREIGN KEY(requester_id) REFERENCES users(id), INDEX ix_matching_requester(requester_id),
 CHECK(preferred_companion_gender IN ('ANY','FEMALE','MALE')),
 CHECK(pace IN ('RELAXED','BALANCED','PACKED')),
 CHECK((budget_min IS NULL AND budget_max IS NULL) OR
       (budget_min IS NOT NULL AND budget_max IS NOT NULL AND budget_min>=0 AND budget_min<=budget_max AND budget_max<=3000000))
) ENGINE=InnoDB;
CREATE TABLE matching_requests_themes (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, matching_request_id BIGINT NOT NULL, theme VARCHAR(20) NOT NULL,
 UNIQUE(matching_request_id,theme), FOREIGN KEY(matching_request_id) REFERENCES matching_requests(id),
 CHECK(theme IN ('NATURE','FOOD','CULTURE','REST','SNS','ACTIVITY'))
) ENGINE=InnoDB;
-- Evaluation tables are AI development tools, never required by runtime SQL.
CREATE TABLE matching_evaluation_cases (
 matching_request_id BIGINT PRIMARY KEY, split VARCHAR(10) NOT NULL,
 scenario VARCHAR(40) NOT NULL, expected_empty BOOLEAN NOT NULL DEFAULT FALSE,
 human_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
 FOREIGN KEY(matching_request_id) REFERENCES matching_requests(id), CHECK(split IN ('DEV','TEST','EDGE'))
) ENGINE=InnoDB;
CREATE TABLE matching_evaluation_labels (
 matching_request_id BIGINT NOT NULL, candidate_id BIGINT NOT NULL,
 relevance TINYINT NOT NULL, reason VARCHAR(255) NOT NULL,
 PRIMARY KEY(matching_request_id,candidate_id),
 FOREIGN KEY(matching_request_id) REFERENCES matching_evaluation_cases(matching_request_id),
 FOREIGN KEY(candidate_id) REFERENCES users(id), CHECK(relevance BETWEEN 0 AND 2)
) ENGINE=InnoDB;
CREATE TABLE matching_dataset_meta (
 id INT PRIMARY KEY, version VARCHAR(40) NOT NULL, seed INT NOT NULL,
 fingerprint CHAR(64) NOT NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
