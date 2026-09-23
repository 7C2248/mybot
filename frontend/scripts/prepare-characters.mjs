// Create a local, ignored asset bundle; never change the original Character directory.
import { readdir, readFile, mkdir, copyFile, writeFile, realpath } from 'node:fs/promises';
import { dirname, resolve, join, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = resolve(frontend, '../Character');
const target = resolve(frontend, 'public/local-characters');
await mkdir(target, { recursive: true });
const characters = [];
async function withinSource(file) {
  const actual = await realpath(file);
  const rel = relative(await realpath(source), actual);
  if (rel.startsWith('..') || isAbsolute(rel)) throw new Error('角色资源必须位于 Character 目录内');
  return actual;
}
let entries = [];
try { entries = await readdir(source, { withFileTypes: true }); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
for (const entry of entries.filter((item) => item.isDirectory()).sort((a, b) => a.name.localeCompare(b.name))) {
  const id = entry.name;
  const folder = join(source, id);
  const profiles = {};
  for (const [language, name] of [['zh', 'profile_cn.md'], ['en', 'profile_en.md']]) {
    try { profiles[language] = await readFile(await withinSource(join(folder, name)), 'utf8'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  if (!Object.keys(profiles).length) continue;
  const assets = [];
  let images = [];
  try { images = await readdir(join(folder, 'images'), { withFileTypes: true }); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  for (const image of images.filter((item) => item.isFile() && /\.(png|jpe?g|webp)$/i.test(item.name))) {
    const assetId = `${id}/${image.name}`;
    const output = join(target, id, image.name);
    await mkdir(dirname(output), { recursive: true });
    await copyFile(await withinSource(join(folder, 'images', image.name)), output);
    assets.push({ id: assetId, name: image.name, url: `/local-characters/${encodeURIComponent(id)}/${encodeURIComponent(image.name)}` });
  }
  characters.push({ id, name: id, profiles, assets });
}
await writeFile(join(target, 'manifest.json'), JSON.stringify(characters, null, 2));
console.log(`已准备 ${characters.length} 个本地角色的档案与图片。`);
