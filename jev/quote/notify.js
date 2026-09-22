// Text the owner every real request with the range attached.
// Uses Twilio when TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM are set; otherwise prints.
export function formatOwnerMessage({ business, text, decision }) {
  const head = `[${business.name}] new request`;
  const range = decision.kind === 'range' ? `${decision.service ?? decision.label}: $${decision.low}–$${decision.high}` : decision.kind === 'visit' ? 'needs a visit (told them: confirm within the hour)' : 'needs a call (told them: confirm within the hour)';
  return `${head}\n${range}\n"${text}"`;
}

export function createNotifier(env = process.env, { fetch: fetchImpl = globalThis.fetch, log = console.log } = {}) {
  const sid = env.TWILIO_ACCOUNT_SID;
  const token = env.TWILIO_AUTH_TOKEN;
  const from = env.TWILIO_FROM;
  return async function notify(event) {
    const body = formatOwnerMessage(event);
    const to = event.business.ownerPhone;
    if (!(sid && token && from && to)) {
      log(`[owner text → ${to ?? 'no ownerPhone'}]\n${body}`);
      return { sent: false, body };
    }
    const url = `https://api.twilio.com/2010-04-01/Accounts/${sid}/Messages.json`;
    const res = await fetchImpl(url, {
      method: 'POST',
      headers: { Authorization: 'Basic ' + Buffer.from(`${sid}:${token}`).toString('base64'), 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ To: to, From: from, Body: body }),
    });
    if (!res.ok) throw new Error(`twilio ${res.status}: ${await res.text()}`);
    return { sent: true, body };
  };
}
