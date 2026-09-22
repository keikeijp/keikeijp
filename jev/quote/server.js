#!/usr/bin/env node
// Serves the widget page and the two API routes. No dependencies.
//   node quote/server.js --config quote/configs/desert-air-hvac.json [--port 3000]
//   JEV_PROVIDER=mock node quote/server.js --config b2b/configs/valley-sign-co.json
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnv } from '../lib/env.js';
import { createClient, JevError } from '../lib/jev.js';
import { createEngine, loadConfig } from './engine.js';
import { createNotifier } from './notify.js';

loadEnv();
const args = process.argv.slice(2);
const flag = (name, def) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : def; };
const configPath = flag('config');
if (!configPath) { console.error('usage: node quote/server.js --config <config.json> [--port 3000]'); process.exit(1); }
const port = Number(flag('port', process.env.PORT ?? 3000));

const config = loadConfig(configPath);
const client = createClient();
const notify = createNotifier();
const engine = createEngine(config, client, { notify });
const html = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'public', 'index.html'));
const status = (config.tableData ?? config.catalogData)?.status ?? 'unknown';

function json(res, code, body) {
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; if (data.length > 20_000) { reject(new Error('body too large')); req.destroy(); } });
    req.on('end', () => { try { resolve(data ? JSON.parse(data) : {}); } catch (e) { reject(e); } });
    req.on('error', reject);
  });
}

export const server = createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && (req.url === '/' || req.url === '/index.html')) {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      return res.end(html);
    }
    if (req.method === 'GET' && req.url === '/api/config') {
      const { name, tagline, phone, bookingUrl } = config.business;
      return json(res, 200, { business: { name, tagline, phone, bookingUrl }, profile: config.profile, status, provider: client.provider });
    }
    if (req.method === 'POST' && req.url === '/api/quote') {
      const { text } = await readBody(req);
      if (!text || typeof text !== 'string' || text.trim().length < 2) return json(res, 400, { kind: 'error', message: 'tell us a bit more' });
      const result = await engine.quote(text.trim().slice(0, 2000));
      console.log(`${new Date().toISOString()} quote "${text.slice(0, 60)}" → ${result.kind}${result.reason ? ` (${result.reason})` : ''} ${Math.round(result.ms)}ms`);
      return json(res, 200, result);
    }
    if (req.method === 'POST' && req.url === '/api/answer') {
      const { pendingId, value } = await readBody(req);
      return json(res, 200, engine.answer(pendingId, value));
    }
    res.writeHead(404); res.end();
  } catch (err) {
    const message = err instanceof JevError ? `${err.name}: ${err.message}` : String(err?.message ?? err);
    console.error(`${new Date().toISOString()} error ${message}`);
    json(res, 502, { kind: 'error', message });
  }
});

server.listen(port, () => {
  console.log(`${config.business.name} widget (${config.profile}) on http://localhost:${port}  provider=${client.provider} table=${status}`);
});
