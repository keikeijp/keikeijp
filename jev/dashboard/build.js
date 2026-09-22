#!/usr/bin/env node
// Inlines dashboard/replay.json into dashboard/index.template.html → dashboard/index.html
//   node dashboard/build.js [--replay dashboard/replay.json] [--out dashboard/index.html]
import { readFileSync, writeFileSync } from 'node:fs';
const args = process.argv.slice(2);
const flag = (name, def) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : def; };
const replay = readFileSync(flag('replay', 'dashboard/replay.json'), 'utf8');
const template = readFileSync('dashboard/index.template.html', 'utf8');
const safe = JSON.stringify(JSON.parse(replay)).replace(/<\/script/gi, '<\\/script');
const out = flag('out', 'dashboard/index.html');
writeFileSync(out, template.replace('/*__REPLAY__*/', safe));
console.log(`${out} (${(Buffer.byteLength(safe) / 1024).toFixed(0)} KB of replay data)`);
