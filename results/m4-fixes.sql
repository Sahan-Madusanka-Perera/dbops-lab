-- Suggested by dbops slow. Review each one; run the SQL ones in that engine's client.

-- oracle
EXEC DBMS_STATS.GATHER_TABLE_STATS('SHOP', 'ORDERS');
CREATE INDEX shop.ix_orders_customer_id_created_at ON shop.orders (customer_id, created_at);
CREATE INDEX shop.ix_orders_created_at ON shop.orders (created_at);
EXEC DBMS_STATS.GATHER_TABLE_STATS('SHOP', 'CUSTOMERS');
CREATE INDEX shop.ix_orders_status ON shop.orders (status);

-- mariadb
ANALYZE TABLE orders;
CREATE INDEX ix_orders_status ON orders (status);
CREATE INDEX ix_orders_created_at ON orders (created_at);

-- mongo
db.orders.createIndex({ customerId: 1, createdAt: -1 })
db.orders.createIndex({ status: 1 })
db.orders.createIndex({ createdAt: -1 })

-- postgres
CREATE INDEX ix_orders_customer_id_created_at ON orders (customer_id, created_at);
CREATE INDEX ix_orders_status ON orders (status);
CREATE INDEX ix_orders_created_at ON orders (created_at);
