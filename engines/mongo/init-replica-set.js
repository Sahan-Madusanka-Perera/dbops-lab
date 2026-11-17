// Initiate rs0, create the accounts, seed shop, turn the profiler on.
//
// Run on mongo1 over localhost, by `make up` and `make accounts`:
//   docker exec -e MONGO_ADMIN_PASSWORD -e APP_PASSWORD -e MONITOR_PASSWORD \
//     dbops-mongo1 mongosh --quiet --file /scripts/init-replica-set.js
//
// The members run with --keyFile, so access control is on from the first start.
// Before any user exists, MongoDB's localhost exception lets a connection from
// the server itself initiate the replica set and create the first user. This
// script uses it once to create dbops_admin, then logs in as dbops_admin for the
// rest. Safe to run again: it initiates once, seeds once, and sets every
// account's roles and password to what is below and in .env.

const env = (name) => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set (see .env.example)`);
  return value;
};
const ADMIN = { user: 'dbops_admin', pwd: env('MONGO_ADMIN_PASSWORD') };

const members = [
  { _id: 0, host: 'mongo1:27017', priority: 2 }, // preferred primary
  { _id: 1, host: 'mongo2:27017', priority: 1 },
  { _id: 2, host: 'mongo3:27017', priority: 1 },
];
const admin = db.getSiblingDB('admin');

const waitForPrimary = () => {
  for (let i = 0; !db.hello().isWritablePrimary; i++) {   // hello needs no login
    if (i === 90) throw new Error('mongo1 is not primary after 90 s');
    sleep(1000);
  }
};

let loggedIn = false;
try {
  loggedIn = admin.auth(ADMIN.user, ADMIN.pwd).ok === 1;
} catch (e) {
  // no such user yet: first run, or a replica set from before access control (M5)
}

if (!db.hello().setName) {
  rs.initiate({ _id: 'rs0', members });
  print('rs0 initiated');
}
waitForPrimary();

if (!loggedIn) {
  admin.createUser({ user: ADMIN.user, pwd: ADMIN.pwd, roles: [{ role: 'root', db: 'admin' }] });
  admin.auth(ADMIN.user, ADMIN.pwd);
  print('dbops_admin created (localhost exception)');
}

// ---------------------------------------------------------------- roles and accounts

const shop = db.getSiblingDB('shop');

// Least privilege for the application: read and write documents, nothing else.
// The built-in readWrite role would also let it create and drop collections and indexes.
const upsertRole = (database, name, privileges) => {
  if (database.getRole(name)) database.updateRole(name, { privileges, roles: [] });
  else database.createRole({ role: name, privileges, roles: [] });
};
upsertRole(shop, 'shopApp', [
  { resource: { db: 'shop', collection: '' }, actions: ['find', 'insert', 'update', 'remove'] },
]);
// dbops slow reads the profiler's output; clusterMonitor does not cover system.profile.
upsertRole(shop, 'profileReader', [
  { resource: { db: 'shop', collection: 'system.profile' }, actions: ['find'] },
]);

const upsertUser = (user, pwd, roles) => {
  if (admin.getUser(user)) admin.updateUser(user, { pwd, roles });
  else admin.createUser({ user, pwd, roles });
  print(`${user} ready: ${roles.map((r) => `${r.role}@${r.db}`).join(', ')}`);
};
upsertUser(ADMIN.user, ADMIN.pwd, [{ role: 'root', db: 'admin' }]);
upsertUser('shop_app', env('APP_PASSWORD'), [{ role: 'shopApp', db: 'shop' }]);
upsertUser('dbops_monitor', env('MONITOR_PASSWORD'), [
  { role: 'clusterMonitor', db: 'admin' },
  { role: 'profileReader', db: 'shop' },
]);

// ---------------------------------------------------------------- seed

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

// ---------------------------------------------------------------- profiler

// Read by `dbops slow` (M4). Per member and not persisted, so it is set on every
// run. Same rule as MariaDB's slow log: slow, or reads 1,000+ documents.
for (const m of members) {
  const conn = new Mongo(m.host);
  conn.getDB('admin').auth(ADMIN.user, ADMIN.pwd);
  conn.getDB('shop').setProfilingLevel(1, {
    filter: { $or: [{ millis: { $gte: 500 } }, { docsExamined: { $gte: 1000 } }] },
  });
}
print('profiler on for shop: millis >= 500 or docsExamined >= 1000');

print(rs.status().members.map((m) => `${m.name} ${m.stateStr}`).join('\n'));
