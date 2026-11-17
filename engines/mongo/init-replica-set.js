// Run by the mongo-init container against mongo1. Safe to run again:
// it initiates rs0 only once and seeds shop only when it is empty.

const members = [
  { _id: 0, host: 'mongo1:27017', priority: 2 }, // preferred primary
  { _id: 1, host: 'mongo2:27017', priority: 1 },
  { _id: 2, host: 'mongo3:27017', priority: 1 },
];

try {
  rs.status();
  print('rs0 already initiated');
} catch (e) {
  if (e.codeName !== 'NotYetInitialized') throw e;
  rs.initiate({ _id: 'rs0', members });
  print('rs0 initiated');
}

// Wait for an election before writing.
for (let i = 0; !db.hello().isWritablePrimary; i++) {
  if (i === 60) throw new Error('no primary elected after 60 s');
  sleep(1000);
}

const shop = db.getSiblingDB('shop');
if (shop.orders.estimatedDocumentCount() === 0) {
  const countries = ['LK', 'SG', 'JP', 'AE'];
  const statuses = (n) => (n % 10 === 0 ? 'REFUNDED' : n % 10 === 1 ? 'PENDING' : 'PAID');
  const now = Date.now();

  shop.customers.insertMany(
    Array.from({ length: 5000 }, (_, i) => ({
      _id: i + 1,
      email: `customer${i + 1}@example.com`,
      country: countries[(i + 1) % 4],
      createdAt: new Date(),
    })),
  );
  shop.customers.createIndex({ email: 1 }, { unique: true });

  // No index on customerId or status: M4 has to find that.
  for (let start = 1; start <= 50000; start += 5000) {
    shop.orders.insertMany(
      Array.from({ length: 5000 }, (_, k) => {
        const n = start + k;
        return {
          customerId: (n % 5000) + 1,
          status: statuses(n),
          totalCents: ((n * 7919) % 50000) + 100,
          createdAt: new Date(now - ((n * 37) % (365 * 24 * 60)) * 60000),
        };
      }),
    );
  }
  print('seeded shop: 5000 customers, 50000 orders');
}

print(rs.status().members.map((m) => `${m.name} ${m.stateStr}`).join('\n'));
