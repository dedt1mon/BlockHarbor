// Public BlockHarbor build directory. D1 holds searchable metadata, R2 holds the snapshots.
const PART = 8 * 1024 ** 2;
const MAX_SIZE = 256 * 1024 ** 2; // 256 MiB maximum per public archive.
const STORAGE_LIMIT = 8 * 1024 ** 3; // Hard ceiling below R2's free allowance.
const ACTIVE_PER_PUBLISHER = 3;
const RATE_WINDOW = 10 * 60;
const RATE_REQUESTS = 10;
const RATE_BAN = 5 * 60 * 60;
const UPLOAD_TTL = 30 * 60; // Unfinished multipart reservations expire in 30 min.
const PUBLIC_TTL = 60 * 60; // Public archives are visible for one hour after completion.
const CLEANUP_BATCH = 16;
const MAX_OWNER_RECEIPTS = 200000; // Bound durable offline receipts without expiring unread events.
const PAGE = 20;
const CATEGORIES = new Set(['adventure','challenging','combat','kitchen-sink','lightweight','magic','multiplayer','optimization','quests','technology']);
const LOADERS = new Set(['vanilla','fabric','quilt','forge','neoforge']);
const FIELDS = 'id, name, category, minecraft, loader, size, sha256, created';
const text = new TextEncoder();
const hex = bytes => Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2,'0')).join('');
const random = length => hex(crypto.getRandomValues(new Uint8Array(length)));
const hash = async value => hex(await crypto.subtle.digest('SHA-256', text.encode(value)));
const now = () => Math.floor(Date.now() / 1000);
const key = row => `builds/${row.id}.zip`;
function json(value, status = 200, extraHeaders = {}) {
  return Response.json(value, {status, headers: {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...extraHeaders
  }});
}
class RequestError extends Error {
  constructor(status, message, details = {}) {
    super(message);
    this.status = status;
    this.details = details;
  }
}
function requireThat(valid, status, message) { if (!valid) throw new RequestError(status, message); }
function publicBuild(row) {
  return {id:row.id, name:row.name, category:row.category, minecraft:row.minecraft,
    loader:row.loader, size:row.size, sha256:row.sha256, expiresAt:row.created+PUBLIC_TTL};
}
function capabilities() {
  return {
    publicRetentionSeconds: PUBLIC_TTL,
    manifestFormat: 1,
    ownerNotificationFormat: 1,
    publishLimits: {
      maxArchiveBytes: MAX_SIZE, maxActive: ACTIVE_PER_PUBLISHER,
      windowSeconds: RATE_WINDOW, maxAttempts: RATE_REQUESTS, banSeconds: RATE_BAN,
    },
  };
}
async function authorized(request, row) {
  const token = /^Bearer ([0-9a-f]{64})$/.exec(request.headers.get('Authorization') || '');
  requireThat(row && token && await hash(token[1]) === row.owner_hash, 403, 'Нет прав на изменение этой публикации.');
}

async function publisherKey(request, env) {
  // Cloudflare sets CF-Connecting-IP at ingress; never use a self-selected
  // installation UUID as the sole rate-limit identity (trivial to rotate).
  const ip = request.headers.get('CF-Connecting-IP');
  requireThat(ip && ip.length<=64 && /^[0-9a-fA-F:.]+$/.test(ip),403,
    'Не удалось подтвердить сетевой адрес для публикации.');
  // The random RATE_SALT is a backend secret. Only a keyed digest of the IP
  // is persisted in D1; raw addresses must not enter logs or SQLite.
  requireThat(typeof env.RATE_SALT==='string' && env.RATE_SALT.length>=32,503,
    'Сервис публикации не настроен. Обратитесь к разработчику лаунчера.');
  const key=await crypto.subtle.importKey('raw',text.encode(env.RATE_SALT),
    {name:'HMAC',hash:'SHA-256'},false,['sign']);
  return hex(await crypto.subtle.sign('HMAC',key,text.encode(ip)));
}
async function checkPublicationRate(env, subject) {
  const moment=now();
  // SQLite UPSERT + RETURNING is one atomic statement, so concurrent POSTs
  // cannot defeat the 10-attempt window. 11th starts a fixed 5-hour block.
  const sql = [
    'INSERT INTO publisher_rate (subject,window_start,attempts,blocked_until,updated_at)',
    'VALUES (?, ?, 1, 0, ?)',
    'ON CONFLICT(subject) DO UPDATE SET',
    'attempts=CASE',
      'WHEN publisher_rate.blocked_until>? THEN publisher_rate.attempts',
      'WHEN publisher_rate.window_start<=?-? THEN 1',
      'ELSE publisher_rate.attempts+1 END,',
    'blocked_until=CASE',
      'WHEN publisher_rate.blocked_until>? THEN publisher_rate.blocked_until',
      'WHEN publisher_rate.window_start> ?-? AND publisher_rate.attempts>=?',
      'THEN ?+?',
      'ELSE 0 END,',
    'window_start=CASE',
      'WHEN publisher_rate.blocked_until>? THEN publisher_rate.window_start',
      'WHEN publisher_rate.window_start<=?-? THEN ?',
      'ELSE publisher_rate.window_start END,',
    'updated_at=?',
    'RETURNING attempts,blocked_until,window_start'
  ].join(' ');
  const result=await env.DB.prepare(sql).bind(
    subject,moment,moment,
    moment,moment,RATE_WINDOW,
    moment,moment,RATE_WINDOW,RATE_REQUESTS,moment,RATE_BAN,
    moment,moment,RATE_WINDOW,moment,moment).first();
  requireThat(result && Number.isInteger(result.attempts),503,
    'Не удалось проверить ограничения публикации.');
  if(result.blocked_until>moment) {
    if(result.blocked_until===moment+RATE_BAN) {
      // Immediately cancel unfinished spam reservations to free R2 capacity.
      // Never unpublish already verified 'ready' builds.
      try {
        const {results:unfinished}=await env.DB.prepare(
          "SELECT * FROM builds WHERE publisher=? AND state='uploading' LIMIT ?"
        ).bind(subject,ACTIVE_PER_PUBLISHER).all();
        for(const row of unfinished) await discard(env,row);
      } catch(error) {
        console.warn('Deferred cleanup for blocked publisher',error?.name || 'error');
      }
    }
    const remaining=result.blocked_until-moment;
    throw new RequestError(429,
      'Публикация временно заблокирована из-за слишком частых попыток.',
      {code:'PUBLISH_RATE_BAN',retryAfterSeconds:remaining,blockedUntil:result.blocked_until});
  }
}
async function cleanupRateCounters(env) {
  const cutoff=now()-RATE_BAN-RATE_WINDOW-3600;
  await env.DB.prepare([
    'DELETE FROM publisher_rate WHERE subject IN',
    '(SELECT subject FROM publisher_rate',
    'WHERE updated_at<? AND blocked_until<? ORDER BY updated_at LIMIT 256)'
  ].join(' ')).bind(cutoff,now()).run();
}

async function discard(env, row, expiredOnly = false) {
  // Hide first. Retries safely finish an interrupted deletion.
  const moment=now();
  const mark="UPDATE builds SET expired_at=CASE WHEN state='ready' AND created<=? THEN created+? ELSE expired_at END,state='removing' WHERE id=?";
  const update = expiredOnly
    ? env.DB.prepare(mark+" AND (state='removing' OR (state='uploading' AND created<?) OR (state='ready' AND created<=?))").bind(moment-PUBLIC_TTL,PUBLIC_TTL,row.id,moment-UPLOAD_TTL,moment-PUBLIC_TTL)
    : env.DB.prepare(mark).bind(moment-PUBLIC_TTL,PUBLIC_TTL,row.id);
  if ((await update.run()).meta.changes === 0) return;
  row=await env.DB.prepare('SELECT * FROM builds WHERE id=?').bind(row.id).first();
  if(!row) return; // Another janitor completed the same idempotent deletion.
  try {
    try { await env.BUCKET.resumeMultipartUpload(key(row), row.multipart).abort(); }
    catch { /* Already completed or automatically expired in R2. */ }
    await env.BUCKET.delete(key(row));
    const statements=[];
    if(row.expired_at!==null) {
      statements.push(env.DB.prepare("INSERT OR IGNORE INTO publication_events(id,owner_hash,expired_at,removed_at) SELECT id,owner_hash,expired_at,? FROM builds WHERE id=? AND state='removing' AND expired_at IS NOT NULL")
        .bind(now(),row.id));
    }
    statements.push(env.DB.prepare('DELETE FROM parts WHERE upload=?').bind(row.upload),
      env.DB.prepare('DELETE FROM builds WHERE id=?').bind(row.id));
    await env.DB.batch(statements);
  } catch(error) {
    // One unavailable R2 object must not starve other expired rows. This retry
    // state is durable and resumes after worker restarts, with a bounded delay.
    const delay=Math.min(3600,30*2**Math.min(row.cleanup_attempts || 0,7));
    await env.DB.prepare('UPDATE builds SET cleanup_after=?,cleanup_attempts=cleanup_attempts+1 WHERE id=?')
      .bind(now()+delay,row.id).run();
    throw error;
  }
}
async function prune(env) {
  const {results} = await env.DB.prepare("SELECT * FROM builds WHERE cleanup_after<=? AND (state='removing' OR (state='uploading' AND created<?) OR (state='ready' AND created<=?)) ORDER BY created,id LIMIT ?").bind(now(),now()-UPLOAD_TTL,now()-PUBLIC_TTL,CLEANUP_BATCH).all();
  // Four bounded deletion lanes, not an unbounded Promise per public archive.
  for(let index=0;index<results.length;index+=4) {
    const deleted=await Promise.allSettled(results.slice(index,index+4).map(row=>discard(env,row,true)));
    for(const result of deleted) if(result.status==='rejected') console.warn('Catalog cleanup deferred',result.reason?.name || 'error');
  }
  return results.length;
}
async function begin(request, env) {
  const length = Number(request.headers.get('Content-Length'));
  requireThat(length > 0 && length <= 4096, 400, 'Некорректный размер описания.');
  const data = await request.json().catch(()=>null);
  const name = typeof data?.name === 'string' ? data.name.trim() : '';
  requireThat(name && Array.from(name).length <= 48 && !/[\u0000-\u001f\u007f]/u.test(name),400,'Укажи название длиной от 1 до 48 символов.');
  const version = data.minecraft;
  const validVersion = typeof version === 'string' && /^[A-Za-z0-9][A-Za-z0-9._+ -]{0,149}$/.test(version)
    && !version.includes('..') && !/[. ]$/.test(version) && !/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\s*\.|$)/i.test(version);
  requireThat(CATEGORIES.has(data.category) && LOADERS.has(data.loader) && validVersion,400,'Неизвестная категория, версия игры или загрузчик.');
  requireThat(Number.isSafeInteger(data.size) && data.size >= 22 && data.size <= MAX_SIZE,413,
    'Размер публичного ZIP не должен превышать 256 МиБ.');
  requireThat(typeof data.sha256==='string' && /^[a-f0-9]{64}$/.test(data.sha256),400,
    'Неверная контрольная сумма сборки.');
  const publisher=await publisherKey(request,env);
  await checkPublicationRate(env,publisher);
  await prune(env);
  const token=random(32), upload=random(16), id='BH-'+random(6).toUpperCase();
  const multi=await env.BUCKET.createMultipartUpload(key({id}));
  try {
    const limit=Math.min(Number(env.CATALOG_STORAGE_LIMIT)||STORAGE_LIMIT,STORAGE_LIMIT);
    const inserted=await env.DB.prepare(`INSERT INTO builds(id,upload,owner_hash,multipart,state,name,search_name,category,minecraft,loader,size,sha256,created,publisher)
      SELECT ?,?,?,?,'uploading',?,?,?,?,?,?,?,?,? WHERE
      (SELECT count(*) FROM builds WHERE publisher=? AND
        ((state='ready' AND created>?) OR (state='uploading' AND created>?)))<? AND
      (SELECT count(*) FROM builds WHERE state IN ('ready','uploading'))<20000 AND
      (SELECT count(*) FROM publication_events)+(SELECT count(*) FROM builds)<? AND
      (SELECT COALESCE(sum(size),0) FROM builds)+?<=?`)
      .bind(id,upload,await hash(token),multi.uploadId,name,name.normalize('NFC').toLowerCase(),data.category,
        data.minecraft,data.loader,data.size,data.sha256,now(),publisher,
        publisher,now()-PUBLIC_TTL,now()-UPLOAD_TTL,ACTIVE_PER_PUBLISHER,
        MAX_OWNER_RECEIPTS,data.size,limit).run();
    requireThat(inserted.meta.changes===1,429,
      'Достигнут лимит: не более 3 активных публикаций с одного адреса либо заполнено хранилище.');
  } catch(error) { await multi.abort(); throw error; }
  return json({upload,token,chunkSize:PART},201);
}
function validateFirstPart(buffer,row) {
  const view=new DataView(buffer), utf8=new TextDecoder('utf-8',{fatal:true});
  let meta;
  try {
    if (buffer.byteLength<30 || view.getUint32(0,true)!==0x04034b50 || view.getUint16(8,true)!==0) throw Error();
    const length=view.getUint16(26,true), extra=view.getUint16(28,true), start=30+length+extra;
    const original=view.getUint32(22,true);
    let size=view.getUint32(18,true);
    if ((view.getUint16(6,true)&9) || start>buffer.byteLength || utf8.decode(new Uint8Array(buffer,30,length))!=='blockharbor.json') throw Error();
    if (size===0xffffffff) { // ZIP64 local header on a small, seekable archive.
      let cursor=30+length;
      while(cursor+4<=start) {
        const type=view.getUint16(cursor,true), bytes=view.getUint16(cursor+2,true);
        if (cursor+4+bytes>start) throw Error();
        if (type===1) {
          const skip=original===0xffffffff ? 8 : 0;
          if (bytes<skip+8) throw Error();
          size=Number(view.getBigUint64(cursor+4+skip,true));
          break;
        }
        cursor+=4+bytes;
      }
    }
    if (size>65536 || start+size>buffer.byteLength) throw Error();
    meta=JSON.parse(utf8.decode(new Uint8Array(buffer,start,size)));
  } catch { throw new RequestError(400,'Нужен архив сборки BlockHarbor.'); }
  requireThat(meta?.format==='BlockHarbor' && meta.formatVersion===1 && meta.worlds===false && meta.name===row.name && meta.minecraft===row.minecraft && meta.loader===row.loader,400,'Описание публикации расходится с манифестом сборки.');
  return meta;
}
async function part(request, env, row, number) {
  requireThat(row.state==='uploading',409,'Загрузка уже завершена.');
  const start=(number-1)*PART;
  requireThat(Number.isSafeInteger(start) && start>=0 && start<row.size,400,'Недопустимый номер части.');
  const size=Math.min(PART,row.size-start);
  requireThat(Number(request.headers.get('Content-Length'))===size,400,'Размер части не совпадает.');
  const buffer=await request.arrayBuffer();
  requireThat(buffer.byteLength===size,400,'Часть загружена не полностью.');
  if(number===1) validateFirstPart(buffer,row);
  const result=await env.BUCKET.resumeMultipartUpload(key(row),row.multipart).uploadPart(number,buffer);
  const saved=await env.DB.prepare("INSERT INTO parts(upload,number,etag) SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM builds WHERE upload=? AND state='uploading') ON CONFLICT(upload,number) DO UPDATE SET etag=excluded.etag")
    .bind(row.upload,number,result.etag,row.upload).run();
  if(!saved.meta.changes) {
    try { await env.BUCKET.resumeMultipartUpload(key(row),row.multipart).abort(); } catch {}
    throw new RequestError(410,'Публикация снята или уже завершена.');
  }
  return json({part:number});
}
// R2 serves only complete catalog archives. Two bounded range reads cover the
// end record and directory even for a multi-gigabyte multipart object.
function validZip(ok) {
  requireThat(ok,422,'Архив сборки повреждён или содержит файлы вне каталога дополнений.');
}
async function zipRange(bucket, row, offset, length) {
  const object = await bucket.get(key(row), {range:{offset,length}});
  requireThat(object,503,'Не удалось прочитать архив из хранилища.');
  const bytes = new Uint8Array(await object.arrayBuffer());
  validZip(bytes.length === length);
  return bytes;
}
const CONFIG_EXT = /\.(?:ini|toml|cfg|conf|json5?|properties|ya?ml|txt|snbt|xml)$/i;
const PRIVATE_CONFIG = /account|auth(?:me|entication|orization|lib)?|credential|passw(?:or)?d|passwd|token|secret|webhook|api[_.-]?key|private|session|nickname|nameprotect|skin(?:ss|loader|restorer)|e4steam|^skins?$|^player(?:data|profiles?|skins?)$|^steam$|^logs?$|^cache$|^user(?:s|cache|name)?$/i;
function checkMember(name, directory, packs) {
  if (name === 'blockharbor.json') return;
  const path = directory ? name.slice(0,-1) : name;
  const [section, ...parts] = path.split('/');
  const config = section === 'config' || section === 'defaultconfigs';
  validZip(config || ['mods','shaderpacks','resourcepacks'].includes(section));
  validZip(Array.from(path).length <= 512 && parts.length < 24);
  for (const part of parts) {
    validZip(part && Array.from(part).length<=180 && !part.startsWith('.') && !/[. ]$/.test(part)
      && !/[<>:"\\|?*\u0000-\u001f\u007f]/u.test(part) && !/^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)/i.test(part));
  }
  if (config) {
    validZip(!parts.some(part=>PRIVATE_CONFIG.test(part)) && (directory || CONFIG_EXT.test(parts.at(-1))));
    return;
  }
  if (!parts.length) { validZip(directory); return; }
  if (parts.length === 1 && !directory) {
    const ext = section === 'mods' ? '.jar' : '.zip', leaf = parts[0].toLowerCase();
    validZip(leaf.endsWith(ext) || leaf.endsWith(ext+'.disabled'));
    return;
  }
  validZip(section !== 'mods');
  const root = section+'/'+parts[0];
  const marker = !directory && (section === 'resourcepacks'
    ? parts.length===2 && parts[1]==='pack.mcmeta'
    : parts.length>=3 && parts[1]==='shaders');
  packs.set(root, packs.get(root) || marker);
}
async function validateDirectory(bucket, row) {
  const length = Math.min(row.size, 65557); // EOCD + longest possible ZIP comment.
  const start = row.size - length;
  const tail = await zipRange(bucket, row, start, length);
  const end = new DataView(tail.buffer, tail.byteOffset, tail.byteLength);
  let pos = length-22;
  while (pos >= 0 && !(end.getUint32(pos,true) === 0x06054b50 && pos+22+end.getUint16(pos+20,true) === length)) pos--;
  validZip(pos >= 0 && !end.getUint16(pos+4,true) && !end.getUint16(pos+6,true));
  const count=end.getUint16(pos+10,true), dirSize=end.getUint32(pos+12,true), dirStart=end.getUint32(pos+16,true);
  validZip(count > 0 && count <= 40000 && count === end.getUint16(pos+8,true)
    && dirSize >= count*46 && dirSize <= 24*1024**2 && dirStart+dirSize === start+pos);
  const central = dirStart >= start ? tail.subarray(dirStart-start,pos) : await zipRange(bucket,row,dirStart,dirSize);
  const view = new DataView(central.buffer,central.byteOffset,central.byteLength), utf8 = new TextDecoder('utf-8',{fatal:true});
  const seen = new Set(), packs = new Map();
  let cursor=0, minimumOffset=0, totalSize=0, hasConfigs=false;
  for(let index=0; index<count; index++) {
    validZip(cursor+46<=central.length && view.getUint32(cursor,true)===0x02014b50);
    const flags=view.getUint16(cursor+8,true), method=view.getUint16(cursor+10,true);
    const packed=view.getUint32(cursor+20,true), size=view.getUint32(cursor+24,true);
    const nameLength=view.getUint16(cursor+28,true), extra=view.getUint16(cursor+30,true), comment=view.getUint16(cursor+32,true);
    const attributes=view.getUint32(cursor+38,true), offset=view.getUint32(cursor+42,true);
    const next=cursor+46+nameLength+extra+comment;
    validZip(next<=central.length && nameLength > 0 && nameLength <= 2048 && !(flags & 1) && (method===0 || method===8)
      && view.getUint16(cursor+34,true)===0 && offset>=minimumOffset && (method!==0 || packed===size));
    let name;
    try { name=utf8.decode(central.subarray(cursor+46,cursor+46+nameLength)); }
    catch { validZip(false); }
    const normalized=name.normalize('NFC').toLowerCase(), directory=name.endsWith('/'), mode=(attributes>>>16)&0xf000;
    validZip(!seen.has(normalized) && !(attributes&0x400) && (mode===0 || mode===(directory ? 0x4000 : 0x8000))
      && (!directory || size===0) && size<=512*1024**2);
    checkMember(name,directory,packs);
    if (/^(?:config|defaultconfigs)\//.test(name)) {
      hasConfigs=true;
      validZip(size<=8*1024**2);
    }
    if(index===0) validZip(name==='blockharbor.json' && offset===0 && size<=65536 && method===0);
    seen.add(normalized);
    // The local header may include extra fields (including a ZIP64 size) and a
    // streaming data descriptor; their lengths need not match central extras.
    minimumOffset=offset+30+nameLength+packed;
    totalSize+=size;
    validZip(minimumOffset<=dirStart && totalSize<=MAX_SIZE && (size<=8*1024**2 || size<=Math.max(packed,1)*350));
    cursor=next;
  }
  validZip(cursor===central.length && [...packs.values()].every(Boolean));
  if (hasConfigs) {
    // The manifest is the first stored entry. Allow for its ZIP64 local extra.
    const first = start===0 ? tail : await zipRange(bucket,row,0,Math.min(row.size,132*1024));
    validZip(validateFirstPart(first.buffer,row).publicConfigs===true);
  }
}
async function finishArchive(env,row) {
  if(row.state==='ready') {
    requireThat(row.created>now()-PUBLIC_TTL,410,'Срок публикации истёк.');
    return json(publicBuild(row));
  }
  requireThat(row.state==='uploading',410,'Публикация снята.');
  let object=await env.BUCKET.get(key(row));
  if(!object) {
    const {results}=await env.DB.prepare('SELECT number AS partNumber, etag FROM parts WHERE upload=? ORDER BY number').bind(row.upload).all();
    requireThat(results.length===Math.ceil(row.size/PART),409,'Ещё не все части загружены.');
    try { await env.BUCKET.resumeMultipartUpload(key(row),row.multipart).complete(results); }
    catch (err) { if (!await env.BUCKET.head(key(row))) throw err; }
    object=await env.BUCKET.get(key(row));
  }
  requireThat(object,503,'Архив временно недоступен.');
  try {
    await validateDirectory(env.BUCKET, row);
  } catch (error) {
    await object.body.cancel();
    if (error instanceof RequestError && error.status === 422) await discard(env,row);
    throw error;
  }
  const digest=new crypto.DigestStream('SHA-256');
  await object.body.pipeTo(digest);
  if(object.size!==row.size || hex(await digest.digest)!==row.sha256) {
    await discard(env,row);
    throw new RequestError(422,'Контрольная сумма архива не совпала. Начни новую публикацию.');
  }
  // Only the initial successful completion starts the hour. Replaying
  // /complete must NEVER extend its expiration (including concurrent calls).
  const publishedAt=now();
  const changed=await env.DB.prepare("UPDATE builds SET state='ready',created=? WHERE id=? AND state='uploading'").bind(publishedAt,row.id).run();
  if(changed.meta.changes===1) return json(publicBuild({...row,created:publishedAt}));
  const current=await env.DB.prepare('SELECT * FROM builds WHERE id=?').bind(row.id).first();
  requireThat(current?.state==='ready' && current.created>now()-PUBLIC_TTL,410,'Публикация снята или истекла.');
  return json(publicBuild(current));
}
async function finish(env,row) {
  try { return await finishArchive(env,row); }
  finally {
    // Withdrawal can race multipart.complete after its initial read. A late R2
    // completion must not recreate an orphan object after the DB row was removed.
    const current=await env.DB.prepare('SELECT state FROM builds WHERE id=?').bind(row.id).first();
    if(!current || current.state==='removing') await env.BUCKET.delete(key(row));
  }
}
async function ownerEvents(request,env,id) {
  let event=await env.DB.prepare('SELECT * FROM publication_events WHERE id=?').bind(id).first();
  const row=await env.DB.prepare('SELECT * FROM builds WHERE id=?').bind(id).first();
  requireThat(event || row,404,'Уведомление публикации не найдено.');
  await authorized(request,event || row);
  if(request.method==='DELETE') {
    requireThat(event,409,'Удаление публикации ещё не подтверждено.');
    await env.DB.prepare('DELETE FROM publication_events WHERE id=?').bind(id).run();
    return json({acknowledged:true});
  }
  requireThat(request.method==='GET',404,'Адрес каталога не найден.');
  if(!event && (row.state==='removing' || row.state==='ready' && row.created<=now()-PUBLIC_TTL)) {
    await discard(env,row,true);
    event=await env.DB.prepare('SELECT * FROM publication_events WHERE id=?').bind(id).first();
  }
  return json({events:event ? [{id:'publication-expired:'+id,type:'publicationExpired',build:id,
    created:event.expired_at,removedAt:event.removed_at}] : []});
}
async function search(env, params) {
  const q=(params.get('q')||'').trim().normalize('NFC').toLowerCase();
  const offset=Number(params.get('offset')||0);
  requireThat(Array.from(q).length<=180 && Number.isSafeInteger(offset) && offset>=0,400,'Некорректный запрос каталога.');
  // Keep expired snapshots hidden even if a cleanup job is delayed.
  try { await prune(env); }
  catch(error) {
    // Cleanup failure must not block a healthy catalog; expiration is enforced
    // independently by the SQL filter and exact-ID read guards.
    console.warn('Catalog expiry cleanup deferred',error?.name || 'error');
  }
  const filters=["state='ready'","created>?"], args=[now()-PUBLIC_TTL];
  if(q) { filters.push('(id=? OR instr(search_name,?)>0)'); args.push(q.toUpperCase(),q); }
  for(const [parameter,column] of [['category','category'],['version','minecraft'],['loader','loader']]) {
    const value=params.get(parameter);
    if(value) { filters.push(column+'=?'); args.push(value); }
  }
  const where=filters.join(' AND ');
  const [count,rows]=await env.DB.batch([env.DB.prepare(`SELECT count(*) AS total FROM builds WHERE ${where}`).bind(...args),
    env.DB.prepare(`SELECT ${FIELDS} FROM builds WHERE ${where} ORDER BY created DESC,id LIMIT ? OFFSET ?`).bind(...args,PAGE,offset)]);
  return json({hits:rows.results.map(publicBuild),total:count.results[0].total,limit:PAGE,offset});
}
async function handle(request,env) {
  const url=new URL(request.url), path=url.pathname, method=request.method;
  if(path==='/' && method==='GET') return json({
    name:'Каталог сборок BlockHarbor',
    releases:'https://github.com/dedt1mon/BlockHarbor/releases',
    api:'/v1/builds', ...capabilities(),
  });
  if(path==='/v1/capabilities' && method==='GET') {
    // Advertise support only if the migration and server secret are present.
    // Merely uploading this source must not make an unconfigured backend
    // appear ready in the launcher.
    requireThat(typeof env.RATE_SALT==='string' && env.RATE_SALT.length>=32 &&
      env.DB && env.BUCKET,503,'Сервис публикации ещё не настроен.');
    await env.DB.prepare('SELECT subject FROM publisher_rate LIMIT 1').first();
    await env.DB.prepare('SELECT cleanup_after,expired_at FROM builds LIMIT 1').first();
    await env.DB.prepare('SELECT id FROM publication_events LIMIT 1').first();
    return json(capabilities());
  }
  if(path==='/v1/builds' && method==='GET') return search(env,url.searchParams);
  if(path==='/v1/uploads' && method==='POST') return begin(request,env);
  const events=/^\/v1\/publications\/(BH-[0-9A-F]{12})\/events$/.exec(path);
  if(events) return ownerEvents(request,env,events[1]);
  const upload=/^\/v1\/uploads\/([a-f0-9]{32})\/(?:parts\/(\d{1,3})|(complete))$/.exec(path);
  if(upload) {
    const row=await env.DB.prepare('SELECT * FROM builds WHERE upload=?').bind(upload[1]).first();
    requireThat(row,404,'Загрузка не найдена.');
    await authorized(request,row);
    requireThat(row.state==='ready' || row.created>=now()-UPLOAD_TTL,410,'Срок загрузки истёк.');
    if(upload[2] && method==='PUT') return part(request,env,row,Number(upload[2]));
    if(upload[3] && method==='POST') return finish(env,row);
  }
  const build=/^\/v1\/builds\/(BH-[0-9A-F]{12})(\/archive)?$/.exec(path);
  if(build) {
    const row=await env.DB.prepare('SELECT * FROM builds WHERE id=?').bind(build[1]).first();
    requireThat(row,404,'Сборка не найдена в каталоге.');
    if(!build[2] && method==='DELETE') { await authorized(request,row); await discard(env,row); return json({removed:true}); }
    requireThat(row.state==='ready' && row.created>now()-PUBLIC_TTL,404,'Срок публикации истёк или сборка недоступна.');
    if(method==='GET' && !build[2]) return json(publicBuild(row));
    if(method==='GET' && build[2]) {
      const file=await env.BUCKET.get(key(row));
      requireThat(file,503,'Архив временно недоступен.');
      return new Response(file.body,{headers:{'Content-Type':'application/octet-stream','Content-Length':String(file.size),
        'X-Content-Type-Options':'nosniff','Content-Disposition':`attachment; filename="${row.id}.zip"`,'Cache-Control':'no-store'}});
    }
  }
  throw new RequestError(404,'Адрес каталога не найден.');
}
export default {
  async fetch(request,env) {
    try {return await handle(request,env);} catch(error) {
      if(error instanceof RequestError) {
        const seconds=error.details.retryAfterSeconds;
        return json({error:error.message,...error.details},error.status,
          Number.isSafeInteger(seconds) && seconds>0 ? {'Retry-After':String(seconds)} : {});
      }
      console.error('Catalog request failed',error.name,error.message);
      return json({error:'Сервис каталога временно недоступен. Повтори запрос.'},503);
    }
  },
  async scheduled(controller,env,ctx) {
    // Cron (configured every minute) reclaims R2 space even when
    // nobody searches or publishes new packs; operations are bounded.
    // <=128 rows keeps D1/R2 calls below the free-plan internal-subrequest
    // budget, including row reads, retry updates and transactional receipts.
    for(let round=0;round<8;round++) {
      const count=await prune(env);
      if(count<CLEANUP_BATCH) break;
    }
    await cleanupRateCounters(env);
  },
};
