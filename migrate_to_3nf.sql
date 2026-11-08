-- migrate_to_3nf.sql
-- Moves the HW5 data into the 3NF schema. setup_schema.py runs these same
-- steps automatically; this file is the plain-SQL version for reference.
--
-- Why: client_ip -> country, so in the HW5 table
--   requests(request_id, country, client_ip, ...)
-- country depended on the key only through client_ip
-- (request_id -> client_ip -> country), a transitive dependency.
-- The fix moves client_ip -> country into its own table, ips.

USE hw5;

-- 0. Check the dependency holds. Any IP listed here has more than one
--    country; in our data these are only manual curl tests (e.g. 1.2.3.4).
SELECT client_ip, GROUP_CONCAT(DISTINCT country) AS countries
FROM requests
GROUP BY client_ip
HAVING COUNT(DISTINCT country) > 1;

-- 1. New tables. (countries and failed_requests are unchanged.)
CREATE TABLE ips (
    client_ip  VARCHAR(45) PRIMARY KEY,
    country    VARCHAR(64) NOT NULL,
    CONSTRAINT fk_ips_country FOREIGN KEY (country) REFERENCES countries(country)
);

CREATE TABLE requests_3nf (
    request_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    client_ip       VARCHAR(45) NOT NULL,
    gender          VARCHAR(16),
    age             VARCHAR(16),
    income          VARCHAR(32),
    time_of_day     VARCHAR(32),
    requested_file  VARCHAR(255) NOT NULL,
    CONSTRAINT fk_requests_client_ip FOREIGN KEY (client_ip) REFERENCES ips(client_ip)
);

-- 2. Copy the data, leaving out IPs that break client_ip -> country.
START TRANSACTION;

INSERT INTO ips (client_ip, country)
SELECT client_ip, MIN(country)
FROM requests
WHERE client_ip NOT IN (
    SELECT client_ip FROM (
        SELECT client_ip FROM requests
        GROUP BY client_ip HAVING COUNT(DISTINCT country) > 1
    ) AS bad
)
GROUP BY client_ip;

INSERT INTO requests_3nf
    (request_id, client_ip, gender, age, income, time_of_day, requested_file)
SELECT request_id, client_ip, gender, age, income, time_of_day, requested_file
FROM requests
WHERE client_ip NOT IN (
    SELECT client_ip FROM (
        SELECT client_ip FROM requests
        GROUP BY client_ip HAVING COUNT(DISTINCT country) > 1
    ) AS bad
);

COMMIT;

-- 3. Check the counts (difference = rows from the conflicting test IPs).
SELECT (SELECT COUNT(*) FROM requests)     AS old_rows,
       (SELECT COUNT(*) FROM requests_3nf) AS new_rows,
       (SELECT COUNT(*) FROM ips)          AS distinct_ips;

-- 4. Swap the tables, keeping the old one as a backup.
RENAME TABLE requests TO requests_hw5, requests_3nf TO requests;

-- Country of any request is now a join away:
-- SELECT r.*, i.country, c.is_banned
-- FROM requests r JOIN ips i USING (client_ip) JOIN countries c USING (country);
