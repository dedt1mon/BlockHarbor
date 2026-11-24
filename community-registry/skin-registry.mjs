// CustomSkinAPI for local BlockHarbor profiles. It does not authenticate a
// Microsoft/Mojang identity; authenticated clients keep Mojang first in CSL.
const MAX_PNG = 24 * 1024;
const MAX_PROFILES = 5000;
const MAX_NETWORK_PROFILES = 32;
const RETENTION = 30 * 24 * 3600;
const RATE_WINDOW = 60;
const encoder = new TextEncoder();
const hex = value => Array.from(new Uint8Array(value), n => n.toString(16).padStart(2, '0')).join('');
const hash = async value => hex(await crypto.subtle.digest('SHA-256',
  typeof value === 'string' ? encoder.encode(value) : value));
const moment = () => Math.floor(Date.now() / 1000);
const headers = {'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'};
const json = (value, status = 200, extra = {}) => Response.json(value,
  {status, headers:{...headers,...extra}});
class SkinError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const requireThat = (valid, status, message) => {
  if (!valid) throw new SkinError(status, message);
};

// CRC validation and an exact decompressed size reject disguised uploads,
// animations, compressed bombs and arbitrary files before storing public data.
const crcTable = Uint32Array.from({length:256}, (_, n) => {
  for (let i=0;i<8;i++) n = (n & 1) ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
  return n >>> 0;
});
function crc32(bytes) {
  let crc=0xffffffff;
  for (const byte of bytes) crc=crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}
async function boundedRead(stream, limit) {
  requireThat(stream,400,'Файл скина отсутствует.');
  const reader=stream.getReader(), chunks=[];
  let size=0;
  try {
    while (true) {
      const {value,done}=await reader.read();
      if (done) break;
      size+=value.byteLength;
      requireThat(size<=limit,413,'Файл скина слишком велик.');
      chunks.push(value);
    }
  } catch(error) {
    await reader.cancel().catch(()=>{});
    throw error;
  } finally { reader.releaseLock(); }
  const result=new Uint8Array(size);
  let offset=0;
  for (const chunk of chunks) { result.set(chunk,offset); offset+=chunk.byteLength; }
  return result;
}
async function validatePNG(bytes) {
  const signature=[137,80,78,71,13,10,26,10];
  requireThat(bytes.length>=57 && signature.every((n,i)=>bytes[i]===n),400,
    'Нужен PNG скина 64×64.');
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength), compressed=[];
  let cursor=8, started=false, ended=false;
  while (cursor<bytes.length) {
    requireThat(cursor+12<=bytes.length,400,'PNG скина повреждён.');
    const size=view.getUint32(cursor), end=cursor+12+size;
    requireThat(end<=bytes.length,400,'PNG скина повреждён.');
    const kind=String.fromCharCode(...bytes.subarray(cursor+4,cursor+8));
    requireThat(view.getUint32(end-4)===crc32(bytes.subarray(cursor+4,end-4)),400,
      'PNG скина не прошёл проверку.');
    if (cursor===8) {
      requireThat(kind==='IHDR' && size===13 && view.getUint32(cursor+8)===64 &&
        view.getUint32(cursor+12)===64 && bytes[cursor+16]===8 &&
        bytes[cursor+17]===6 && bytes[cursor+18]===0 && bytes[cursor+19]===0 &&
        bytes[cursor+20]===0,400,'Нужен обычный PNG RGBA 64×64.');
    } else if (kind==='IDAT') {
      requireThat(!ended,400,'PNG скина повреждён.');
      started=true;
      compressed.push(bytes.subarray(cursor+8,end-4));
    } else if (kind==='IEND') {
      requireThat(started && !ended && size===0 && end===bytes.length,400,
        'PNG скина повреждён.');
      ended=true;
    } else throw new SkinError(400,'Скин должен содержать только пиксели PNG.');
    cursor=end;
  }
  requireThat(ended,400,'PNG скина повреждён.');
  let pixels;
  try {
    pixels=await boundedRead(new Blob(compressed).stream()
      .pipeThrough(new DecompressionStream('deflate')),64*(1+64*4));
  } catch { throw new SkinError(400,'Данные пикселей PNG повреждены.'); }
  requireThat(pixels.length===64*257 &&
    Array.from({length:64},(_,row)=>pixels[row*257]).every(filter=>filter<=4),400,
    'Данные пикселей PNG повреждены.');
}
async function ownerKey(request) {
  const token=/^Bearer ([a-f0-9]{64})$/.exec(request.headers.get('Authorization') || '');
  requireThat(token,403,'Нет ключа владельца скина.');
  return hash(token[1]);
}
async function admit(env, owner, publisher) {
  const time=moment(), subjects=[await hash('skin-owner:'+owner),await hash('skin-ip:'+publisher)];
  await env.DB.prepare('DELETE FROM skin_rate WHERE updated_at<?').bind(time-24*3600).run();
  const {results}=await env.DB.prepare(`
    WITH allowed AS MATERIALIZED (
      SELECT 1 WHERE NOT EXISTS (
        SELECT 1 FROM skin_rate WHERE window_start>? AND
        ((subject=? AND attempts>=3) OR (subject=? AND attempts>=30))
      ) AND (SELECT count(*) FROM skin_rate)<20000
    )
    INSERT INTO skin_rate(subject,window_start,attempts,updated_at)
      SELECT subject,?,1,? FROM (SELECT ? AS subject UNION ALL SELECT ?)
      WHERE EXISTS (SELECT 1 FROM allowed)
    ON CONFLICT(subject) DO UPDATE SET
      attempts=CASE WHEN skin_rate.window_start<=? THEN 1 ELSE skin_rate.attempts+1 END,
      window_start=CASE WHEN skin_rate.window_start<=? THEN excluded.window_start ELSE skin_rate.window_start END,
      updated_at=excluded.updated_at
    RETURNING subject
  `).bind(time-RATE_WINDOW,...subjects,time,time,...subjects,
    time-RATE_WINDOW,time-RATE_WINDOW).all();
  requireThat(results.length===2,429,'Слишком частая смена скина. Повтори через минуту.');
}
async function publish(request, env, name, publisherKey) {
  const model=request.headers.get('X-Skin-Model'), owner=await ownerKey(request);
  requireThat(model==='classic' || model==='slim',400,'Неизвестная модель скина.');
  requireThat(request.headers.get('Content-Type')==='image/png',415,'Нужен PNG скина.');
  const length=Number(request.headers.get('Content-Length'));
  requireThat(Number.isSafeInteger(length) && length>0 && length<=MAX_PNG,413,
    'Неверный размер PNG скина.');
  const bytes=await boundedRead(request.body,MAX_PNG);
  requireThat(bytes.length===length,400,'PNG скина получен не полностью.');
  await validatePNG(bytes);
  const username=name.toLowerCase(), digest=await hash(bytes), time=moment();
  const prior=await env.DB.prepare('SELECT owner_hash,sha256,model,updated_at FROM skin_profiles WHERE username=?')
    .bind(username).first();
  requireThat(!prior || prior.owner_hash===owner || prior.updated_at<=time-RETENTION,409,
    'Этот ник уже используется другим профилем BlockHarbor. Выбери другой ник для общего скина.');
  const publisher=await publisherKey(request,env);
  // Lost replies and launches with an unchanged skin are idempotent; they do
  // not consume the edit allowance or write on every game start.
  if (prior?.owner_hash===owner && prior.sha256===digest && prior.model===model) {
    if (prior.updated_at<time-3600) await env.DB.prepare(
      'UPDATE skin_profiles SET updated_at=? WHERE username=? AND owner_hash=?'
    ).bind(time,username,owner).run();
    return json({username:name,sha256:digest,model,
      expiresAt:(prior.updated_at<time-3600 ? time : prior.updated_at)+RETENTION});
  }
  await admit(env,owner,publisher);
  await env.DB.prepare('DELETE FROM skin_profiles WHERE updated_at<=?').bind(time-RETENTION).run();
  try {
    const result=await env.DB.prepare(`INSERT INTO skin_profiles
      (username,display_name,owner_hash,publisher,model,sha256,png,updated_at)
      SELECT ?,?,?,?,?,?,?,? WHERE
        EXISTS (SELECT 1 FROM skin_profiles WHERE owner_hash=?) OR
        ((SELECT count(*) FROM skin_profiles)<? AND
         (SELECT count(*) FROM skin_profiles WHERE publisher=?)<?)
      ON CONFLICT(owner_hash) DO UPDATE SET username=excluded.username,
        display_name=excluded.display_name,publisher=excluded.publisher,
        model=excluded.model,sha256=excluded.sha256,png=excluded.png,updated_at=excluded.updated_at
    `).bind(username,name,owner,publisher,model,digest,bytes.buffer,time,owner,
      MAX_PROFILES,publisher,MAX_NETWORK_PROFILES).run();
    requireThat(result.meta.changes===1,429,'Сервис скинов временно заполнен. Локальный скин сохранён.');
  } catch(error) {
    if (/unique constraint|constraint.*skin_profiles.username/i.test(error?.message || '')) {
      throw new SkinError(409,'Этот ник уже используется другим профилем BlockHarbor. Выбери другой ник для общего скина.');
    }
    throw error;
  }
  return json({username:name,sha256:digest,model,expiresAt:time+RETENTION});
}

export async function skinRegistry(request, env, {publisherKey}) {
  const url=new URL(request.url), path=url.pathname, method=request.method;
  if (!path.startsWith('/v1/skins/')) return null;
  try {
    requireThat(!url.search,400,'Параметры адреса скина не поддерживаются.');
    if (path==='/v1/skins/capabilities' && method==='GET') {
      requireThat(env.DB && typeof env.RATE_SALT==='string' && env.RATE_SALT.length>=32,
        503,'Сервис скинов ещё не настроен.');
      await env.DB.prepare('SELECT username,sha256 FROM skin_profiles LIMIT 1').first();
      await env.DB.prepare('SELECT subject FROM skin_rate LIMIT 1').first();
      return json({skinApiFormat:1,maxPngBytes:MAX_PNG,retentionSeconds:RETENTION});
    }
    const profile=/^\/v1\/skins\/([A-Za-z0-9_]{3,16})\.json$/.exec(path);
    if (profile && method==='GET') {
      const row=await env.DB.prepare('SELECT display_name,model,sha256 FROM skin_profiles WHERE username=? AND updated_at>?')
        .bind(profile[1].toLowerCase(),moment()-RETENTION).first();
      return json(row ? {username:row.display_name,skins:{[row.model==='slim'?'slim':'default']:row.sha256}}
        : {username:profile[1],skins:{}});
    }
    const texture=/^\/v1\/skins\/textures\/([a-f0-9]{64})$/.exec(path);
    if (texture && method==='GET') {
      const row=await env.DB.prepare('SELECT png FROM skin_profiles WHERE sha256=? AND updated_at>? LIMIT 1')
        .bind(texture[1],moment()-RETENTION).first();
      requireThat(row,404,'Скин не найден.');
      const bytes=new Uint8Array(row.png);
      return new Response(bytes,{headers:{...headers,'Content-Type':'image/png',
        'Content-Length':String(bytes.byteLength),'Cache-Control':'public, max-age=3600, immutable',
        ETag:'"'+texture[1]+'"'}});
    }
    const edit=/^\/v1\/skins\/profile\/([A-Za-z0-9_]{3,16})$/.exec(path);
    if (edit && method==='PUT') return await publish(request,env,edit[1],publisherKey);
    if (edit && method==='DELETE') {
      const owner=await ownerKey(request), username=edit[1].toLowerCase();
      const row=await env.DB.prepare('SELECT owner_hash FROM skin_profiles WHERE username=?').bind(username).first();
      requireThat(!row || row.owner_hash===owner,403,'Этот скин принадлежит другому профилю.');
      await env.DB.prepare('DELETE FROM skin_profiles WHERE username=? AND owner_hash=?').bind(username,owner).run();
      return json({removed:true});
    }
    return json({error:'Адрес скина не найден.'},404);
  } catch(error) {
    if (error instanceof SkinError) return json({error:error.message},error.status);
    throw error; // Shared Worker handles unavailable bindings without exposing internals.
  }
}
