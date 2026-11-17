-- Runs once, on the first start of an empty volume, as SYS.
-- The SHOP user itself is created by the image from APP_USER / APP_USER_PASSWORD.
WHENEVER SQLERROR EXIT SQL.SQLCODE

ALTER SESSION SET CONTAINER = FREEPDB1;

-- Small and fixed-size on purpose: the full-tablespace drill (M7) fills it,
-- and the health check (M2) has to notice before it does.
CREATE TABLESPACE app_data
  DATAFILE '/opt/oracle/oradata/FREE/FREEPDB1/app_data01.dbf'
  SIZE 20M AUTOEXTEND OFF;

ALTER USER shop DEFAULT TABLESPACE app_data QUOTA UNLIMITED ON app_data;

CREATE TABLE shop.customers (
  id         NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email      VARCHAR2(120) NOT NULL UNIQUE,
  country    CHAR(2)       NOT NULL,
  created_at TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL
) TABLESPACE app_data;

-- No index on customer_id or status: M4 has to find that.
CREATE TABLE shop.orders (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id NUMBER       NOT NULL REFERENCES shop.customers (id),
  status      VARCHAR2(10) NOT NULL,
  total_cents NUMBER(10)   NOT NULL,
  created_at  TIMESTAMP    NOT NULL
) TABLESPACE app_data;

INSERT INTO shop.customers (email, country)
SELECT 'customer' || LEVEL || '@example.com',
       CASE MOD(LEVEL, 4) WHEN 0 THEN 'LK' WHEN 1 THEN 'SG' WHEN 2 THEN 'JP' ELSE 'AE' END
FROM dual CONNECT BY LEVEL <= 5000;

INSERT INTO shop.orders (customer_id, status, total_cents, created_at)
SELECT MOD(LEVEL, 5000) + 1,
       CASE MOD(LEVEL, 10) WHEN 0 THEN 'REFUNDED' WHEN 1 THEN 'PENDING' ELSE 'PAID' END,
       MOD(LEVEL * 7919, 50000) + 100,
       SYSTIMESTAMP - NUMTODSINTERVAL(MOD(LEVEL * 37, 365 * 24 * 60), 'MINUTE')
FROM dual CONNECT BY LEVEL <= 50000;

COMMIT;
