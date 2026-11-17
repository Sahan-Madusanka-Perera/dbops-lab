// The application's view of a MongoDB failover, for drills/mongo-primary-lost.sh.
//
// Runs inside a replica set member's container (so the members' Docker names
// resolve), connected as shop_app with a replica set connection string, the way
// the application connects: the driver finds the primary and follows elections.
// Writes one small document every 100 ms with w: "majority" and reports how long
// writes could not be acknowledged.
//
// Env: APP_PASSWORD, WRITER_SECONDS (how long to run), WRITER_TAG (labels the documents).

const seconds = Number(process.env.WRITER_SECONDS || 40);
const tag = process.env.WRITER_TAG || 'drill';
const uri = 'mongodb://mongo1:27017,mongo2:27017,mongo3:27017/shop?replicaSet=rs0&authSource=admin'
  + '&serverSelectionTimeoutMS=30000&retryWrites=true';

const conn = new Mongo(uri);
const shop = conn.getDB('shop');
shop.getSiblingDB('admin').auth('shop_app', process.env.APP_PASSWORD);
const coll = shop.getCollection('drill_writes');

const iso = (ms) => new Date(ms).toISOString().slice(11, 23);
const end = Date.now() + seconds * 1000;
let ok = 0;
let failed = 0;
let lastOk = null;
let worst = { gap: 0, from: null, to: null };
let lastError = '';

while (Date.now() < end) {
  const started = Date.now();
  try {
    coll.insertOne({ tag, at: new Date(started) }, { writeConcern: { w: 'majority', wtimeout: 10000 } });
    const done = Date.now();
    if (lastOk !== null && done - lastOk > worst.gap) worst = { gap: done - lastOk, from: lastOk, to: done };
    if (lastOk !== null && done - lastOk > 1000) {
      print(`${iso(done)}  writes acknowledged again after ${((done - lastOk) / 1000).toFixed(1)} s without one`);
    }
    lastOk = done;
    ok += 1;
  } catch (e) {
    failed += 1;
    const msg = (e.codeName || e.name) + ': ' + String(e.message).split('\n')[0].slice(0, 120);
    if (msg !== lastError) print(`${iso(Date.now())}  write failed: ${msg}`);
    lastError = msg;
  }
  sleep(100);
}

print(`writer (${tag}): ${ok} writes acknowledged, ${failed} failed, over ${seconds} s`);
if (worst.from !== null) {
  print(`longest time without an acknowledged write: ${(worst.gap / 1000).toFixed(1)} s `
    + `(${iso(worst.from)} to ${iso(worst.to)} UTC)`);
}
