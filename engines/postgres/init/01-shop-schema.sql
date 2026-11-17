-- Runs once, on the first start of an empty volume, in the shop database.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE customers (
  id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email      varchar(120) NOT NULL UNIQUE,
  country    char(2)      NOT NULL,
  created_at timestamptz  NOT NULL DEFAULT now()
);

-- No index on customer_id or status: M4 has to find that.
CREATE TABLE orders (
  id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id integer     NOT NULL REFERENCES customers (id),
  status      varchar(10) NOT NULL,
  total_cents integer     NOT NULL,
  created_at  timestamptz NOT NULL
);

INSERT INTO customers (email, country)
SELECT 'customer' || n || '@example.com',
       (ARRAY['LK', 'SG', 'JP', 'AE'])[n % 4 + 1]
FROM generate_series(1, 5000) AS n;

INSERT INTO orders (customer_id, status, total_cents, created_at)
SELECT n % 5000 + 1,
       CASE n % 10 WHEN 0 THEN 'REFUNDED' WHEN 1 THEN 'PENDING' ELSE 'PAID' END,
       (n::bigint * 7919) % 50000 + 100,
       now() - make_interval(mins => (n * 37) % (365 * 24 * 60))
FROM generate_series(1, 50000) AS n;

ANALYZE;
