// Public BlockHarbor build directory. D1 holds searchable metadata, R2 holds the snapshots.
const PART = 8 * 1024 ** 2;
const MAX_SIZE = 2 * 1024 ** 3;
const DAY = 86400;
const PAGE = 20;
const CATEGORIES = new Set(['adventure','challenging','combat','kitchen-sink','lightweight','magic','multiplayer','optimization','quests','technology']);
const LOADERS = new Set(['vanilla','fabric','quilt','forge','neoforge']);
const FIELDS = 'id, name, category, minecraft, loader, size, sha256';
const text = new TextEncoder();
const hex = bytes => Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2,'0')).join('');
const random = length => hex(crypto.getRandomValues(new Uint8Array(length)));
const hash = async value => hex(await crypto.subtle.digest('SHA-256', text.encode(value)));
const now = () => Math.floor(Date.now() / 1000);
const key = row => `builds/${row.id}.zip`;
function json(value, status = 200) {
  return Response.json(value, {status, headers: {'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
}
class RequestError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
function requireThat(valid, status, message) { if (!valid) throw new RequestError(status, message); }
function publicBuild(row) {
  return {id:row.id, name:row.name, category:row.category, minecraft:row.minecraft,
    loader:row.loader, size:row.size, sha256:row.sha256};
}
async function authorized(request, row) {
  const token = /^Bearer ([0-9a-f]{64})$/.exec(request.headers.get('Authorization') || '');
  requireThat(row && token && await hash(token[1]) === row.owner_hash, 403, 'Нет прав на изменение этой публикации.');
}
async function discard(env, row, expiredOnly = false) {
  // Hide first. Retries safely finish an interrupted deletion.
  const update = expiredOnly
    ? env.DB.prepare("UPDATE builds SET state='removing' WHERE id=? AND (state='removing' OR (state='uploading' AND created<?))").bind(row.id, now()-DAY)
    : env.DB.prepare("UPDATE builds SET state='removing' WHERE id=?").bind(row.id);
  if ((await update.run()).meta.changes === 0) return;
  if (row.state !== 'ready') {
    try { await env.BUCKET.resumeMultipartUpload(key(row), row.multipart).abort(); }
    catch { /* Already completed or automatically expired in R2. */ }
  }
  await env.BUCKET.delete(key(row));
  await env.DB.batch([env.DB.prepare('DELETE FROM parts WHERE upload=?').bind(row.upload),
    env.DB.prepare('DELETE FROM builds WHERE id=?').bind(row.id)]);
}
async function prune(env) {
  const {results} = await env.DB.prepare("SELECT * FROM builds WHERE state='removing' OR (state='uploading' AND created<?) LIMIT 4").bind(now()-DAY).all();
  for (const row of results) await discard(env,row,true);
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
  requireThat(Number.isSafeInteger(data.size) && data.size >= 22 && data.size <= MAX_SIZE && typeof data.sha256 === 'string' && /^[0-9a-f]{64}$/.test(data.sha256),400,'Неверный размер архива или SHA-256.');
  await prune(env);
  const token=random(32), upload=random(16), id='BH-'+random(6).toUpperCase();
  const publisher=await hash(`${Math.floor(now()/DAY)}:${request.headers.get('CF-Connecting-IP') || 'anonymous'}`);
  const multi=await env.BUCKET.createMultipartUpload(key({id}));
  try {
    const inserted=await env.DB.prepare(`INSERT INTO builds(id,upload,owner_hash,multipart,state,name,search_name,category,minecraft,loader,size,sha256,created,publisher)
      SELECT ?,?,?,?,'uploading',?,?,?,?,?,?,?,?,? WHERE
      (SELECT count(*) FROM builds WHERE publisher=? AND created>?)<40 AND
      (SELECT count(*) FROM builds)<20000 AND (SELECT COALESCE(sum(size),0) FROM builds)+?<=?`)
      .bind(id,upload,await hash(token),multi.uploadId,name,name.normalize('NFC').toLowerCase(),data.category,data.minecraft,data.loader,data.size,data.sha256,now(),publisher,publisher,now()-DAY,data.size,Number(env.CATALOG_STORAGE_LIMIT) || 10*1024**3).run();
    requireThat(inserted.meta.changes===1,429,'Лимит публикаций или доступного места исчерпан. Попробуй позже.');
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
  await env.DB.prepare('INSERT INTO parts(upload,number,etag) VALUES(?,?,?) ON CONFLICT(upload,number) DO UPDATE SET etag=excluded.etag')
    .bind(row.upload,number,result.etag).run();
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
async function finish(env,row) {
  if(row.state==='ready') return json(publicBuild(row));
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
  const changed=await env.DB.prepare("UPDATE builds SET state='ready' WHERE id=? AND state IN ('uploading','ready')").bind(row.id).run();
  requireThat(changed.meta.changes===1,410,'Публикация снята.');
  return json(publicBuild(row));
}
async function search(env, params) {
  const q=(params.get('q')||'').trim().normalize('NFC').toLowerCase();
  const offset=Number(params.get('offset')||0);
  requireThat(Array.from(q).length<=180 && Number.isSafeInteger(offset) && offset>=0,400,'Некорректный запрос каталога.');
  const filters=["state='ready'"], args=[];
  if(q) { filters.push('(id=? OR instr(search_name,?)>0)'); args.push(q.toUpperCase(),q); }
  for(const [parameter,column] of [['category','category'],['version','minecraft'],['loader','loader']]) {
    const value=params.get(parameter);
    if(value) { filters.push(column+'=?'); args.push(value); }
  }
  const where=filters.join(' AND ');
  const [count,rows]=await env.DB.batch([env.DB.prepare(`SELECT count(*) AS total FROM builds WHERE ${where}`).bind(...args),
    env.DB.prepare(`SELECT ${FIELDS} FROM builds WHERE ${where} ORDER BY created DESC,id LIMIT ? OFFSET ?`).bind(...args,PAGE,offset)]);
  return json({hits:rows.results,total:count.results[0].total,limit:PAGE,offset});
}
async function handle(request,env) {
  const url=new URL(request.url), path=url.pathname, method=request.method;
  if(path==='/' && method==='GET') return json({name:'Каталог сборок BlockHarbor',releases:'https://github.com/dedt1mon/BlockHarbor/releases',api:'/v1/builds'});
  if(path==='/v1/builds' && method==='GET') return search(env,url.searchParams);
  if(path==='/v1/uploads' && method==='POST') return begin(request,env);
  const upload=/^\/v1\/uploads\/([a-f0-9]{32})\/(?:parts\/(\d{1,3})|(complete))$/.exec(path);
  if(upload) {
    const row=await env.DB.prepare('SELECT * FROM builds WHERE upload=?').bind(upload[1]).first();
    requireThat(row,404,'Загрузка не найдена.');
    await authorized(request,row);
    requireThat(row.state==='ready' || row.created>=now()-DAY,410,'Срок загрузки истёк.');
    if(upload[2] && method==='PUT') return part(request,env,row,Number(upload[2]));
    if(upload[3] && method==='POST') return finish(env,row);
  }
  const build=/^\/v1\/builds\/(BH-[0-9A-F]{12})(\/archive)?$/.exec(path);
  if(build) {
    const row=await env.DB.prepare('SELECT * FROM builds WHERE id=?').bind(build[1]).first();
    requireThat(row,404,'Сборка не найдена в каталоге.');
    if(!build[2] && method==='DELETE') { await authorized(request,row); await discard(env,row); return json({removed:true}); }
    requireThat(row.state==='ready',404,'Сборка не найдена в каталоге.');
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
export default {async fetch(request,env) {
  try {return await handle(request,env);} catch(error) {
    if(error instanceof RequestError) return json({error:error.message},error.status);
    console.error('Catalog request failed',error.name,error.message);
    return json({error:'Сервис каталога временно недоступен. Повтори запрос.'},503);
  }
}};
