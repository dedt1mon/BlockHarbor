// Run with Node 22.13+ and Python 3. No npm packages or cloud credentials required.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import worker from './worker.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const migration = readFileSync(new URL('drizzle/0000_catalog.sql', import.meta.url), 'utf8');
const abuseMigration = readFileSync(new URL('drizzle/0001_abuse_guard.sql', import.meta.url), 'utf8')
  .replaceAll('--> statement-breakpoint', '');
const sha = value => createHash('sha256').update(value).digest('hex');
const partSize = 8 * 1024 ** 2;
const buildName = 'Моя сборка';
const metadata = bytes => ({
  name: buildName, category: 'adventure', minecraft: '1.20.1', loader: 'fabric', size: bytes.length, sha256: sha(bytes),
});

if (!crypto.DigestStream) {
  crypto.DigestStream = class extends WritableStream {
    constructor() {
      const hash = createHash('sha256');
      let done;
      const result = new Promise(resolve => { done = resolve; });
      super({write: chunk => { hash.update(chunk); }, close: () => done(hash.digest())});
      this.digest = result;
    }
  };
}

// Use the production exporter so archive changes exercise the server contract.
function exportBuild(large = false) {
  const python = `
import sys, tempfile, zipfile
from io import BytesIO
from pathlib import Path
from launcher.store import Store
from launcher.build_archive import export_build
with tempfile.TemporaryDirectory() as temp:
    store = Store(Path(temp))
    pid = store.create('Local', '1.20.1', 'fabric')
    folder = store.folder(pid)
    with zipfile.ZipFile(folder/'mods'/'Зимний мод.jar.disabled', 'w') as mod:
        mod.writestr('fabric.mod.json', '{"id":"test"}')
    with zipfile.ZipFile(folder/'resourcepacks'/'Ресурсы.zip', 'w') as pack:
        pack.writestr('pack.mcmeta', '{}')
        if sys.argv[1] == 'large':
            pack.writestr('assets/test/data.bin', b'x'*(8*1024**2))
    stream = BytesIO()
    export_build(store, pid, stream, catalog_name=sys.argv[2])
    sys.stdout.buffer.write(stream.read())
`;
  return execFileSync(process.env.PYTHON || 'python3', ['-c', python, large ? 'large' : 'small', buildName],
    {cwd: root, maxBuffer: 12 * 1024 ** 2});
}
const small = exportBuild();

class D1Fixture {
  constructor() {
    this.db = new DatabaseSync(':memory:');
    this.db.exec('PRAGMA foreign_keys = ON');
    this.db.exec(migration);
    this.db.exec(abuseMigration);
  }
  prepare(sql) {
    const statement = this.db.prepare(sql);
    const bound = (args = []) => ({
      bind: (...values) => bound(values),
      first: async () => statement.get(...args),
      all: async () => ({results: statement.all(...args)}),
      run: async () => ({meta:{changes: statement.run(...args).changes}}),
      execute: () => (statement.columns().length ? {results:statement.all(...args)} : {meta:statement.run(...args)}),
    });
    return bound();
  }
  async batch(statements) {
    this.db.exec('BEGIN');
    try {
      const result = statements.map(statement => statement.execute());
      this.db.exec('COMMIT');
      return result;
    } catch(error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }
}

class R2Fixture {
  objects = new Map();
  uploads = new Map();
  async createMultipartUpload(key) {
    const id = crypto.randomUUID();
    this.uploads.set(id, new Map());
    return this.resumeMultipartUpload(key,id);
  }
  resumeMultipartUpload(key,uploadId) {
    return {
      uploadId,
      uploadPart: async (number,buffer) => {
        const bytes = Buffer.from(buffer), etag = sha(bytes);
        this.uploads.get(uploadId).set(number,{bytes, etag});
        return {partNumber:number, etag};
      },
      complete: async parts => {
        const stored = this.uploads.get(uploadId);
        if (!stored) throw Error('Upload already closed');
        const buffers = parts.map(({partNumber,etag}) => {
          const part = stored.get(partNumber);
          assert.equal(part.etag, etag);
          return part.bytes;
        });
        this.objects.set(key,Buffer.concat(buffers));
        this.uploads.delete(uploadId);
      },
      abort: async () => this.uploads.delete(uploadId),
    };
  }
  async get(key, options) {
    const object = this.objects.get(key);
    if (!object) return null;
    const range = options?.range;
    const data = range ? object.subarray(range.offset, range.offset+range.length) : object;
    const response = new Response(data);
    return {size:object.length, body:response.body, arrayBuffer:() => response.arrayBuffer()};
  }
  async head(key) { return this.objects.has(key) ? {size:this.objects.get(key).length} : null; }
  async delete(key) { this.objects.delete(key); }
}
function registry(t) {
  const env = {DB:new D1Fixture(), BUCKET:new R2Fixture(),
    RATE_SALT:'integration-test-only-long-random-test-salt-2026'};
  t.after(() => env.DB.db.close());
  const call = (method, path, body, token, {ip='203.0.113.10'}={}) => {
    const headers = {'CF-Connecting-IP':ip};
    if (body) headers['Content-Length'] = String(body.length);
    if (token) headers.Authorization = 'Bearer '+token;
    return worker.fetch(new Request('https://catalog.test/v1/'+path,{method, body, headers}),env);
  };
  const begin = (data, opts) => call('POST','uploads',Buffer.from(JSON.stringify(data)),undefined,opts);
  async function open(data) {
    const response = await begin(data);
    assert.equal(response.status,201);
    const {upload,token} = await response.json();
    return {
      put:(n,body) => call('PUT',`uploads/${upload}/parts/${n}`,body,token),
      finish:() => call('POST',`uploads/${upload}/complete`,undefined,token),
      remove:id => call('DELETE','builds/'+id,undefined,token),
    };
  }
  return {env,call,begin,open};
}

test('multipart export, retries, concurrent completion, search, archive, and owner withdrawal',async t => {
  const api = registry(t), archive = exportBuild(true), upload = await api.open(metadata(archive));
  const first = archive.subarray(0,partSize), last = archive.subarray(partSize);
  assert.equal((await upload.put(2,last)).status,200);
  assert.equal((await upload.finish()).status,409);
  assert.equal((await upload.put(1,first)).status,200);
  assert.equal((await upload.put(1,first)).status,200);
  const results = await Promise.all([upload.finish(),upload.finish()]);
  for (const response of results) assert.equal(response.status,200);
  const listing = await results[0].json();
  assert.deepEqual(await results[1].json(),listing);
  assert.deepEqual(await (await upload.finish()).json(), listing);
  const page=await (await api.call('GET','builds?q='+encodeURIComponent('СБОР')+'&loader=fabric')).json();
  assert.deepEqual(page.hits,[listing]);
  assert.deepEqual(await (await api.call('GET','builds/'+listing.id)).json(),listing);
  const file=await api.call('GET','builds/'+listing.id+'/archive');
  assert.equal(file.status,200);
  assert.equal(sha(new Uint8Array(await file.arrayBuffer())),sha(archive));
  assert.equal((await api.call('DELETE','builds/'+listing.id)).status,403);
  assert.equal((await upload.remove(listing.id)).status,200);
  assert.equal((await upload.remove(listing.id)).status,404);
  assert.equal((await api.call('GET','builds/'+listing.id+'/archive')).status,404);
  assert.equal(api.env.BUCKET.objects.size,0);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM parts').get().n,0);
});

test('invalid public metadata, incomplete ZIP and checksum failure never become searchable',async t => {
  const api = registry(t);
  const mismatch = await api.open({...metadata(small),name:'Другая сборка'});
  assert.equal((await mismatch.put(1,small)).status,400);
  for (const [bytes,digest] of [[small.subarray(0,-22),sha(small.subarray(0,-22))],[small,'0'.repeat(64)]]) {
    const upload = await api.open({...metadata(bytes),sha256:digest});
    assert.equal((await upload.put(1,bytes)).status,200);
    assert.equal((await upload.finish()).status,422);
    assert.equal(api.env.BUCKET.objects.size,0);
  }
  assert.equal((await (await api.call('GET','builds')).json()).total,0);
});

test('expired incomplete uploads are pruned and reservations obey storage budget',async t => {
  const api = registry(t);
  const upload = await api.open(metadata(small));
  assert.equal((await upload.put(1,small)).status,200);
  api.env.CATALOG_STORAGE_LIMIT=small.length;
  assert.equal((await api.begin(metadata(small))).status,429);
  assert.equal(api.env.BUCKET.uploads.size,1);
  api.env.DB.db.exec('UPDATE builds SET created=0');
  assert.equal((await upload.finish()).status,410);
  assert.equal((await api.begin(metadata(small))).status,201);
  assert.equal(api.env.BUCKET.uploads.size,1);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM parts').get().n,0);
});

test('directory packs, ZIP64 local headers and opt-in configuration files', async t => {
  const api = registry(t);
  function fixture(configMode, includeMarker) {
    const py = `
import io,json,sys,zipfile
stream=io.BytesIO()
meta=dict(name=sys.argv[1],minecraft="1.20.1",loader="fabric",format="BlockHarbor",formatVersion=1,worlds=False,publicConfigs=(sys.argv[2]=="opt-in"))
with zipfile.ZipFile(stream,"w") as z:
    with z.open("blockharbor.json","w",force_zip64=True) as m:
        m.write(json.dumps(meta,ensure_ascii=False).encode())
    z.writestr("shaderpacks/Custom/shaders/main.vsh","test")
    z.writestr("resourcepacks/Краски/","")
    if sys.argv[3]=="yes":
        z.writestr("resourcepacks/Краски/pack.mcmeta","{}")
    z.writestr("resourcepacks/Краски/assets/test/lang/ru_ru.json","{}")
    z.writestr("defaultconfigs/visuals.toml","particles=true")
sys.stdout.buffer.write(stream.getvalue())
`;
    return execFileSync(process.env.PYTHON || 'python3',['-c',py,buildName,configMode,includeMarker ? 'yes' : 'no']);
  }
  for (const [mode,marker,expected] of [['opt-in',true,200],['off',true,422],['opt-in',false,422]]) {
    const bytes=fixture(mode,marker), upload=await api.open(metadata(bytes));
    assert.equal((await upload.put(1,bytes)).status,200);
    assert.equal((await upload.finish()).status,expected);
  }
});

test('completion resumes from a previously completed R2 object and withdrawal wins a concurrent check',async t => {
  const api=registry(t), upload=await api.open(metadata(small));
  assert.equal((await upload.put(1,small)).status,200);
  const row=api.env.DB.db.prepare('SELECT * FROM builds').get();
  const parts=api.env.DB.db.prepare('SELECT number AS partNumber,etag FROM parts ORDER BY number').all();
  await api.env.BUCKET.resumeMultipartUpload('builds/'+row.id+'.zip',row.multipart).complete(parts);
  const get=api.env.BUCKET.get.bind(api.env.BUCKET);
  const reached=Promise.withResolvers(), resume=Promise.withResolvers();
  api.env.BUCKET.get=async (key,opts) => {
    const object=await get(key,opts);
    if(opts?.range) { reached.resolve(); await resume.promise; }
    return object;
  };
  const completing=upload.finish();
  await reached.promise;
  assert.equal((await upload.remove(row.id)).status,200);
  resume.resolve();
  assert.equal((await completing).status,410);
  assert.equal(api.env.BUCKET.objects.size,0);
  assert.equal((await (await api.call('GET','builds')).json()).total,0);
});


test('public snapshot expires exactly one hour after completion and replay cannot renew it',async t => {
  const api=registry(t), author=await api.open(metadata(small));
  assert.equal((await author.put(1,small)).status,200);
  const first=await (await author.finish()).json();
  const row=api.env.DB.db.prepare('SELECT created,state FROM builds WHERE id=?').get(first.id);
  assert.equal(row.state,'ready');
  assert.equal(first.expiresAt,row.created+3600);
  assert.deepEqual(await (await author.finish()).json(),first);
  const page=await (await api.call('GET','builds')).json();
  assert.deepEqual(page.hits,[first]);
  const fetched=await api.call('GET','builds/'+first.id+'/archive');
  assert.equal(fetched.status,200);
  assert.equal(sha(new Uint8Array(await fetched.arrayBuffer())),sha(small));
  api.env.DB.db.prepare("UPDATE builds SET created=created-3600 WHERE id=?").run(first.id);
  assert.equal((await author.finish()).status,410);
  assert.equal((await api.call('GET','builds/'+first.id+'/archive')).status,404);
  const expiredPage=await (await api.call('GET','builds')).json();
  assert.equal(expiredPage.total,0);
  assert.equal(api.env.BUCKET.objects.size,0);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM builds').get().n,0);
});

test('scheduled janitor deletes expired R2 objects without catalog traffic',async t => {
  const api=registry(t), upload=await api.open(metadata(small));
  await upload.put(1,small);
  const listing=await (await upload.finish()).json();
  api.env.DB.db.exec("UPDATE builds SET created=0 WHERE state='ready'");
  assert.equal(api.env.BUCKET.objects.size,1);
  await worker.scheduled({cron:'*/5 * * * *'},api.env,{waitUntil(){}});
  assert.equal(api.env.BUCKET.objects.size,0);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM builds').get().n,0);
  assert.equal((await api.call('GET','builds/'+listing.id)).status,404);
});

test('expired publications free space before next upload reservation',async t => {
  const api=registry(t), author=await api.open(metadata(small));
  await author.put(1,small);
  await author.finish();
  api.env.CATALOG_STORAGE_LIMIT=small.length;
  assert.equal((await api.begin(metadata(small))).status,429);
  api.env.DB.db.exec("UPDATE builds SET created=0 WHERE state='ready'");
  assert.equal((await api.begin(metadata(small))).status,201);
  assert.equal(api.env.BUCKET.objects.size,0);
});



test('256 MiB cap, storage reserve, and per-network active-slot limit',async t => {
  const api=registry(t);
  const big=await api.begin({...metadata(small),size:256*1024*1024+1});
  assert.equal(big.status,413);
  assert.match((await big.json()).error,/256 МиБ/);
  const allowed=await api.begin({...metadata(small),size:256*1024*1024});
  assert.equal(allowed.status,201); // metadata only; no large file allocated
  const reserve=await api.begin(metadata(small));
  assert.equal(reserve.status,201);
  const third=await api.begin(metadata(small));
  assert.equal(third.status,201);
  const fourth=await api.begin(metadata(small));
  assert.equal(fourth.status,429);
  assert.match((await fourth.json()).error,/3 активных/);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM builds').get().n,3);
  const otherNetwork=await api.begin(metadata(small),{ip:'198.51.100.44'});
  assert.equal(otherNetwork.status,201);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM builds').get().n,4);
  assert.equal(api.env.DB.db.prepare('SELECT length(subject) AS n FROM publisher_rate LIMIT 1').get().n,64);
  const raw=api.env.DB.db.prepare('SELECT subject FROM publisher_rate LIMIT 1').get().subject;
  assert.ok(!raw.includes('203.0.113.10'));
});

test('11th attempt in ten minutes triggers five-hour ban and Retry-After',async t => {
  const api=registry(t), attempts=[];
  for(let i=0;i<10;i++) attempts.push(await api.begin(metadata(small)));
  assert.equal(attempts.slice(0,3).filter(r=>r.status===201).length,3);
  assert.equal(attempts.slice(3).filter(r=>r.status===429).length,7);
  const eleventh=await api.begin(metadata(small));
  assert.equal(eleventh.status,429);
  const info=await eleventh.json();
  assert.equal(info.code,'PUBLISH_RATE_BAN');
  assert.ok(info.retryAfterSeconds>0 && info.retryAfterSeconds<=5*3600);
  assert.ok(info.blockedUntil>Math.floor(Date.now()/1000));
  assert.equal(eleventh.headers.get('Retry-After'),String(info.retryAfterSeconds));
  const row=api.env.DB.db.prepare('SELECT * FROM publisher_rate').get();
  assert.equal(row.attempts,11);
  assert.equal(row.blocked_until,info.blockedUntil);
  const again=await (await api.begin(metadata(small))).json();
  assert.equal(again.blockedUntil,info.blockedUntil); // hammering cannot extend ban
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM builds').get().n,3);
  assert.equal(api.env.BUCKET.uploads.size,3);
  api.env.DB.db.exec('DELETE FROM builds'); // simulate TTL cleanup before later retry
  api.env.DB.db.exec('UPDATE publisher_rate SET blocked_until=0,window_start=0');
  const recovered=await api.begin(metadata(small));
  assert.equal(recovered.status,201);
  const reset=api.env.DB.db.prepare('SELECT attempts,blocked_until FROM publisher_rate').get();
  assert.equal(reset.attempts,1);
  assert.equal(reset.blocked_until,0);
});

test('concurrent publication bursts remain atomic even with many simultaneous clients',async t => {
  const api=registry(t);
  const results=await Promise.all(Array.from({length:16},()=>api.begin(metadata(small))));
  assert.equal(results.filter(x=>x.status===201).length,3);
  const blocked=await Promise.all(results.map(async x=>x.status===429?await x.json():{}));
  assert.ok(blocked.some(x=>x.code==='PUBLISH_RATE_BAN'));
  assert.equal(api.env.DB.db.prepare('SELECT attempts FROM publisher_rate').get().attempts,11);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM builds').get().n,3);
});

test('missing edge identity or secret fails closed only for publishing',async t => {
  const api=registry(t);
  api.env.RATE_SALT='';
  assert.equal((await api.begin(metadata(small))).status,503);
  assert.equal((await api.call('GET','builds')).status,200);
  api.env.RATE_SALT='integration-test-only-long-random-test-salt-2026';
  const body=Buffer.from(JSON.stringify(metadata(small)));
  const withoutIp=await worker.fetch(new Request('https://catalog.test/v1/uploads',{
    method:'POST',body,headers:{'Content-Length':String(body.length)}}),api.env);
  assert.equal(withoutIp.status,403);
  assert.equal(api.env.DB.db.prepare('SELECT count(*) AS n FROM publisher_rate').get().n,0);
});
