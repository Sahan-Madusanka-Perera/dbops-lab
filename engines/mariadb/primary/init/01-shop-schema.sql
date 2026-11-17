-- Runs once, on the first start of an empty volume. The shop database and
-- user come from MARIADB_DATABASE / MARIADB_USER.
USE shop;

CREATE TABLE customers (
  id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  email      VARCHAR(120) NOT NULL UNIQUE,
  country    CHAR(2)      NOT NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE = InnoDB;

-- No index on customer_id or status: M4 has to find that.
CREATE TABLE orders (
  id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  customer_id INT UNSIGNED NOT NULL,
  status      VARCHAR(10)  NOT NULL,
  total_cents INT UNSIGNED NOT NULL,
  created_at  DATETIME     NOT NULL,
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
) ENGINE = InnoDB;

-- seq_1_to_N comes from MariaDB's built-in Sequence engine.
INSERT INTO customers (email, country)
SELECT CONCAT('customer', seq, '@example.com'),
       ELT(seq % 4 + 1, 'LK', 'SG', 'JP', 'AE')
FROM seq_1_to_5000;

INSERT INTO orders (customer_id, status, total_cents, created_at)
SELECT seq % 5000 + 1,
       CASE seq % 10 WHEN 0 THEN 'REFUNDED' WHEN 1 THEN 'PENDING' ELSE 'PAID' END,
       (seq * 7919) % 50000 + 100,
       NOW() - INTERVAL ((seq * 37) % (365 * 24 * 60)) MINUTE
FROM seq_1_to_50000;
