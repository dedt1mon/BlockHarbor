import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {deflateSync} from 'node:zlib';
import {skinRegistry} from './skin-registry.mjs';

const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
function crc(bytes) {
  let value=0xffffffff;
  for (const byte of bytes) {
    value^=byte;
    for (let i=0;i<8;i++) value=value&1 ? 0xedb88320^(value>>>1) : value>>>1;
  }
  return (value^0xffffffff)>>>0;
}
function chunk(type,body) {
  const data=Buffer.concat([Buffer.from(type),body]), header=Buffer.alloc(4), checksum=Buffer.alloc(4);
  header.writeUInt32BE(body.length); checksum.writeUInt32BE(crc(data));
  return Buffer.concat([header,data,checksum]);
}
function png(color=128,extra=0) {
  const ihdr=Buffer.alloc(13); ihdr.writeUInt32BE(64); ihdr.writeUInt32BE(64,4); ihdr[8]=8; ihdr[9]=6;
  const pixels=Buffer.alloc(64*257+extra,color);
  for(let row=0;row<64;row++) pixels[row*257]=0;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',ihdr),
    chunk('IDAT',deflateSync(pixels)),chunk('IEND',Buffer.alloc(0))]);
}
function registry(t) {
  const db=new DatabaseSync(':memory:');
  db.exec(readFileSync(new URL('./drizzle/0004_skin_registry.sql',import.meta.url),'utf8').replaceAll('--> statement-breakpoint',''));
  t.after(()=>db.close());
  const env={RATE_SALT:'test-only-skin-registry-salt-value',DB:{prepare(sql) {
    const statement=db.prepare(sql);
    const bound=(values=[])=>({bind:(...args)=>bound(args.map(arg=>arg instanceof ArrayBuffer?new Uint8Array(arg):arg)),
      first:async()=>statement.get(...values),all:async()=>({results:statement.all(...values)}),
      run:async()=>({meta:{changes:statement.run(...values).changes}})});
    return bound();
  }}};
  const call=(method,path,{token='a'.repeat(64),body,model='classic',ip='test-network'}={})=>{
    const headers={'Authorization':'Bearer '+token,'X-Skin-Model':model};
    if(body) Object.assign(headers,{'Content-Type':'image/png','Content-Length':String(body.length)});
    return skinRegistry(new Request('https://skins.invalid'+path,{method,headers,body}),env,
      {publisherKey:async()=>digest(ip)});
  };
  return {call,db};
}

test('two clients exchange exact PNG and model; owner rename atomically removes old name',async t=>{
  const {call,db}=registry(t), bytes=png();
  const capabilities=await call('GET','/v1/skins/capabilities');
  assert.equal((await capabilities.json()).skinApiFormat,1);
  const upload=await call('PUT','/v1/skins/profile/DedTimon',{body:bytes,model:'slim'});
  assert.equal(upload.status,200);
  const receipt=await upload.json(); assert.equal(receipt.sha256,digest(bytes));
  const profile=await (await call('GET','/v1/skins/dedtimon.json',{token:'b'.repeat(64)})).json();
  assert.deepEqual(profile,{username:'DedTimon',skins:{slim:digest(bytes)}});
  const texture=await call('GET','/v1/skins/textures/'+profile.skins.slim);
  assert.equal(texture.headers.get('Content-Type'),'image/png');
  assert.deepEqual(Buffer.from(await texture.arrayBuffer()),bytes);
  assert.equal((await call('PUT','/v1/skins/profile/NewName',{body:bytes})).status,200);
  assert.deepEqual((await (await call('GET','/v1/skins/DedTimon.json')).json()).skins,{});
  assert.deepEqual((await (await call('GET','/v1/skins/NewName.json')).json()).skins,{default:digest(bytes)});
  assert.equal(db.prepare('SELECT count(*) AS n FROM skin_profiles').get().n,1);
  assert.equal((await call('DELETE','/v1/skins/profile/NewName')).status,200);
  assert.equal((await call('GET','/v1/skins/textures/'+digest(bytes))).status,404);
});

test('name ownership and concurrent name collision cannot overwrite or delete another player',async t=>{
  const {call,db}=registry(t), original=png(32), second=png(64), token='b'.repeat(64);
  assert.equal((await call('PUT','/v1/skins/profile/PlayerOne',{body:original})).status,200);
  assert.equal((await call('PUT','/v1/skins/profile/PlayerTwo',{body:second,token})).status,200);
  assert.equal((await call('PUT','/v1/skins/profile/PlayerOne',{body:second,token})).status,409);
  assert.equal((await call('DELETE','/v1/skins/profile/PLAYERONE',{token})).status,403);
  assert.equal(db.prepare('SELECT sha256 FROM skin_profiles WHERE username=?').get('playerone').sha256,digest(original));
  assert.equal(db.prepare('SELECT sha256 FROM skin_profiles WHERE username=?').get('playertwo').sha256,digest(second));
});

test('invalid PNG, metadata, oversized inflate and unsafe paths are rejected before public storage',async t=>{
  const {call,db}=registry(t), broken=png(); broken[40]^=1;
  for(const bytes of [Buffer.from('not PNG'),broken,png(128,1024)]) {
    assert.equal((await call('PUT','/v1/skins/profile/Player',{body:bytes})).status,400);
  }
  assert.equal((await call('PUT','/v1/skins/profile/Player',{body:png(),model:'unknown'})).status,400);
  assert.equal((await call('GET','/v1/skins/textures/anything.png')).status,404);
  assert.equal((await call('GET','/v1/skins/Player.json?url=file:///etc/passwd')).status,400);
  assert.equal(db.prepare('SELECT count(*) AS n FROM skin_profiles').get().n,0);
});

test('edits have an owner cooldown, retries are idempotent and abandoned skins expire',async t=>{
  const {call,db}=registry(t);
  for(const color of [32,64,96]) assert.equal((await call('PUT','/v1/skins/profile/Player',{body:png(color)})).status,200);
  assert.equal((await call('PUT','/v1/skins/profile/Player',{body:png(96)})).status,200);
  assert.equal((await call('PUT','/v1/skins/profile/Player',{body:png(128)})).status,429);
  db.prepare('UPDATE skin_profiles SET updated_at=0').run();
  assert.deepEqual((await (await call('GET','/v1/skins/Player.json')).json()).skins,{});
  assert.equal((await call('GET','/v1/skins/textures/'+digest(png(96)))).status,404);
});
