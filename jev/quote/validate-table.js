#!/usr/bin/env node
// Checks a price table (trade) or catalog (b2b) before it goes in front of a customer:
//   - every row has low <= high
//   - size / quantity bands do not overlap and are in order
//   - every service/item cites its sources; fewer than 3 sources -> reported as "drop"
//   - status is "sample" until sources are real, and the widget shows a banner while it is
// Usage: node quote/validate-table.js <file.json> [more.json...]
import { readFileSync } from 'node:fs';

export function validate(data) {
  const problems = [];
  const drops = [];
  const minSources = data.minSources ?? 3;
  if (data.services) {
    const sizes = data.sizes ?? [];
    let last = -Infinity;
    for (const s of sizes) {
      if (s.maxSqft != null) {
        if (s.maxSqft <= last) problems.push(`size band "${s.name}" overlaps or is out of order (maxSqft ${s.maxSqft} <= ${last})`);
        last = s.maxSqft;
      }
    }
    for (const [key, svc] of Object.entries(data.services)) {
      const sources = svc.sources ?? [];
      if (sources.length < minSources) drops.push(`${key}: ${sources.length} source(s), needs ${minSources}`);
      for (const [size, byUrg] of Object.entries(svc.rows ?? {})) {
        if (!sizes.some((s) => s.name === size)) problems.push(`${key}: unknown size "${size}"`);
        for (const [urg, [low, high]] of Object.entries(byUrg)) {
          if (!(data.urgencies ?? []).some((u) => u.name === urg)) problems.push(`${key}: unknown urgency "${urg}"`);
          if (!(low >= 0 && high >= low)) problems.push(`${key}/${size}/${urg}: bad range [${low}, ${high}]`);
        }
      }
    }
  }
  if (data.items) {
    const bands = data.quantityBands ?? [];
    let last = 0;
    for (const b of bands) {
      if (b.min !== last + 1) problems.push(`quantity band "${b.name}" starts at ${b.min}, expected ${last + 1} (bands must be contiguous and not overlap)`);
      if (b.max != null && b.max < b.min) problems.push(`quantity band "${b.name}" has max < min`);
      last = b.max ?? Infinity;
    }
    for (const [key, item] of Object.entries(data.items)) {
      const sources = item.sources ?? [];
      if (sources.length < minSources) drops.push(`${key}: ${sources.length} source(s), needs ${minSources}`);
      for (const [band, [low, high]] of Object.entries(item.bands ?? {})) {
        if (!bands.some((b) => b.name === band)) problems.push(`${key}: unknown band "${band}"`);
        if (!(low >= 0 && high >= low)) problems.push(`${key}/${band}: bad range [${low}, ${high}]`);
      }
    }
  }
  return { ok: problems.length === 0, problems, drops, status: data.status ?? 'unknown' };
}

if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split('/').pop())) {
  let failed = false;
  for (const file of process.argv.slice(2)) {
    const r = validate(JSON.parse(readFileSync(file, 'utf8')));
    console.log(`${file}: ${r.ok ? 'OK' : 'PROBLEMS'}  (status: ${r.status})`);
    for (const p of r.problems) console.log(`  ✗ ${p}`);
    for (const d of r.drops) console.log(`  drop: ${d}`);
    if (!r.ok) failed = true;
  }
  process.exit(failed ? 1 : 0);
}
