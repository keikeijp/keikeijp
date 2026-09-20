// Vercel AI Gateway 経由で Jev を呼ぶ最小例 (AI SDK 7.0.105 以降の experimental_evaluate)。
//
//   npm install
//   export AI_GATEWAY_API_KEY=...      # https://vercel.com/ai-gateway で発行。ブラウザ側に埋め込まない
//   npm start
//
// AI SDK の質問型は TypeSafe 公式と名前が少し違う:
//   noul   -> { type: 'boolean', instructions }              回答: answers.x.probability (真である確率)
//   choice -> { type: 'choice',  instructions, criteria: {} } 回答: answers.x.choice / probabilities
//   score  -> { type: 'score',   instructions, criteria: [] } 回答: answers.x.score / probabilities
//
// Python 側 (jev_usecases/usecases/triage.py) と同じ質問を送っている。

import { experimental_evaluate as evaluate } from 'ai';

const ticket = {
  subject: 'Charged twice',
  body: '請求が二重になっています。返金をお願いします。',
  customer_tier: 'business',
};

const result = await evaluate({
  model: 'typesafe-ai/jev',
  state: ticket,
  questions: {
    refundRequested: {
      type: 'boolean',
      instructions: 'Is the customer asking for a refund or a reversal of a charge?',
    },
    department: {
      type: 'choice',
      instructions: 'Which department should handle this ticket?',
      criteria: {
        billing: 'Charges, invoices, refunds, duplicate payments, subscription changes.',
        bug: 'Something is broken, an error message, a crash, data not saving.',
        howto: 'Asking how to use a feature; nothing is broken.',
        account: 'Login, password reset, email change, permissions.',
        other: 'None of the other departments clearly fits.',
      },
    },
    urgency: {
      type: 'score',
      instructions: 'How urgent is this ticket?',
      criteria: [
        'Can wait: no deadline, general question.',
        'This week: the customer is inconvenienced but working.',
        'Today: the customer is blocked or losing money.',
        'Right now: outage, security incident, or data loss in progress.',
      ],
    },
  },
});

const { refundRequested, department, urgency } = result.answers;
console.log({
  refundRequested: refundRequested.probability >= 0.5,
  refundProbability: refundRequested.probability,
  department: department.choice,
  departmentProbabilities: department.probabilities,
  urgencyLevel: Math.round(urgency.score),
  usage: result.usage,
});
