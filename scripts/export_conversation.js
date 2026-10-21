/**
 * 把 WorkBuddy 会话 jsonl 导出为可读的 Markdown 对话实录。
 *
 * 用法: node export_conversation.js <会话jsonl路径> <输出md路径>
 *
 * ★ 脱敏：会话日志里包含 MCP Token 等敏感信息，导出时统一替换。
 *   这是本文件进入公开仓库前的必要步骤。
 */

const fs = require('fs');
const path = require('path');

const src = process.argv[2];
const out = process.argv[3];
if (!src || !out) {
  console.error('用法: node export_conversation.js <in.jsonl> <out.md>');
  process.exit(1);
}

// ---- 脱敏规则 ----
// ★ 本文件会进入公开仓库，因此**不能写入任何真实 token 字符串**。
// 完整 token 由调用方通过环境变量 MCD_REDACT_TOKENS 传入（逗号分隔）。
//
// 难点：会话日志里 token 可能被**任意切分**存储（实测见过源码里写成
// ["UQcdIytlzwch", "Uscck6of6En84LPy2ux1"] 两段拼接的形式），
// 只匹配完整串会漏。因此从完整 token **派生所有 >= MIN_FRAG_LEN 位的连续子串**，
// 一次性覆盖所有切分方式。
const MIN_FRAG_LEN = 7;

// 完整 token 从环境变量读取
const FULL_SECRETS = (process.env.MCD_REDACT_TOKENS || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

// 从完整 token 派生子串集合
const FRAGMENTS = new Set();
for (const sec of FULL_SECRETS) {
  for (let i = 0; i < sec.length; i++) {
    for (let j = i + MIN_FRAG_LEN; j <= sec.length; j++) {
      const sub = sec.slice(i, j);
      const hasLetter = /[A-Za-z]/.test(sub);
      const hasDigit = /\d/.test(sub);
      // 含数字的片段：>= 7 位即可
      // 纯字母片段：要求 >= 10 位，避免误伤普通英文单词
      if (hasLetter && hasDigit) FRAGMENTS.add(sub);
      else if (hasLetter && sub.length >= 10) FRAGMENTS.add(sub);
    }
  }
}
// 长片段优先替换，避免部分重叠
const FRAGMENT_LIST = [...FRAGMENTS].sort((a, b) => b.length - a.length);

const PATTERNS = [
  // 任意 Bearer 头后跟长串
  /(Bearer\s+)([A-Za-z0-9]{16,})/g,
  // 环境变量赋值形式
  /(MCD_MCP_TOKEN\s*=\s*["']?)([A-Za-z0-9]{16,})(["']?)/g,
  // 32 位十六进制（可能是 id / token）
  /\b[0-9a-fA-F]{32}\b/g,
];

function desensitize(text) {
  if (!text) return text;
  let s = text;
  // 1) 完整串
  for (const sec of FULL_SECRETS) {
    while (s.includes(sec)) s = s.replace(sec, '<MCP_TOKEN_REDACTED>');
  }
  // 2) 派生片段（覆盖任意切分形式）
  for (const frag of FRAGMENT_LIST) {
    while (s.includes(frag)) s = s.replace(frag, '<TOKEN_REDACTED>');
  }
  // 2b) 清理「检测命令里残留的片段」：
  //     对话中出现过形如 grep -qE "UQcdIytlzwch|..." 的检测命令文本，
  //     这些不是真实 token，但形态上仍像敏感串，统一收敛。
  s = s.replace(/[A-Za-z0-9]{7,20}(?=\|)/g, (m) => {
    if (FRAGMENTS.has(m)) return '<TOKEN_REDACTED>';
    return m;
  });
  s = s.replace(/(["'`]\s*,\s*)([A-Za-z0-9]{7,20})(?=\s*,\s*["'`])/g, (m, p1, p2) =>
    FRAGMENTS.has(p2) ? p1 + '<TOKEN_REDACTED>' : m);
  s = s.replace(PATTERNS[0], (m, p1) => p1 + '<TOKEN_REDACTED>');
  s = s.replace(PATTERNS[1], (m, p1, p3) => p1 + '<TOKEN_REDACTED>' + p3);
  s = s.replace(PATTERNS[2], '<HASH_REDACTED>');
  return s;
}

let redactionCount = 0;
function redact(text) {
  const before = text;
  const s = desensitize(text);
  if (s !== before) redactionCount++;
  return s;
}

const lines = fs.readFileSync(src, 'utf8').split(/\r?\n/).filter(Boolean);
const records = [];
for (const l of lines) {
  try { records.push(JSON.parse(l)); } catch (e) { /* 跳过坏行 */ }
}

function ts(t) {
  if (!t) return '';
  const d = new Date(typeof t === 'number' ? t : Number(t));
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function textOf(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const c of content) {
    if (!c || typeof c !== 'object') continue;
    if (c.type === 'text' && c.text) parts.push(c.text);
    else if (c.type === 'thinking' && c.thinking) parts.push('_(推理)_ ' + c.thinking);
    else if (c.type === 'tool_use' || c.type === 'tool_result') { /* 工具消息单独处理 */ }
    else if (c.text) parts.push(c.text);
  }
  return parts.join('\n\n');
}

// 收集工具调用名，用于给函数调用配名字
const toolNames = new Map();
for (const r of records) {
  if (r.type === 'function_call' && r.name) toolNames.set(r.id, r.name);
}

const out_lines = [];
let stat = { user: 0, assistant: 0, tool: 0, reasoning: 0 };

for (const r of records) {
  if (r.type === 'message') {
    const role = r.role || (r.message && r.message.role) || 'unknown';
    const msg = r.message || r;
    const content = msg.content;
    const t = textOf(content);
    const at = ts(r.timestamp);

    if (role === 'user') {
      let clean = t.replace(/<system-reminder[\s\S]*?<\/system-reminder>/g, '').trim();
      // 去掉包裹标签，只留正文
      clean = clean.replace(/^<user_query>\s*/i, '').replace(/\s*<\/user_query>$/i, '');
      clean = clean.replace(/<\/?(user_query|system-reminder|command-name)>/gi, '').trim();
      if (!clean) continue;
      stat.user++;
      out_lines.push(`\n### 👤 用户 · ${at}\n\n${redact(clean)}\n`);
    } else if (role === 'assistant') {
      if (!t.trim()) continue;
      stat.assistant++;
      out_lines.push(`\n### 🤖 阿册 · ${at}\n\n${redact(t.trim())}\n`);
    }
  } else if (r.type === 'reasoning') {
    stat.reasoning++;
    const t = textOf(r.content) || (r.text || '');
    if (t.trim()) {
      out_lines.push(`\n<details><summary>🧠 推理过程 · ${ts(r.timestamp)}</summary>\n\n${redact(t.trim())}\n\n</details>\n`);
    }
  } else if (r.type === 'function_call') {
    stat.tool++;
    const name = r.name || 'unknown';
    let args = '';
    try {
      const a = typeof r.arguments === 'string' ? JSON.parse(r.arguments) : (r.arguments || {});
      // 截断超长参数
      const s = JSON.stringify(a, null, 1);
      args = s.length > 1200 ? s.slice(0, 1200) + '\n  …（已截断）' : s;
    } catch (e) {
      args = String(r.arguments || '').slice(0, 600);
    }
    out_lines.push(`\n<details><summary>🔧 工具调用 · ${name} · ${ts(r.timestamp)}</summary>\n\n\`\`\`json\n${redact(args)}\n\`\`\`\n\n</details>\n`);
  }
}

const header = `# WorkBuddy 开发对话实录 · 麦门店店价差官

> 本文件由 WorkBuddy 会话日志自动导出，用于核验项目确实由腾讯 WorkBuddy 智能体开发。
>
> 会话 ID：\`6c50a5d3-00c7-4137-8a63-2522341bbde9\`
> 工作目录：\`I:\\worbuddy项目\\麦当劳\`
> 导出时间：${new Date().toLocaleString('zh-CN')}
> 统计：用户 ${stat.user} 条 · 助手 ${stat.assistant} 条 · 工具调用 ${stat.tool} 次 · 推理块 ${stat.reasoning} 个

---

## 对话正文
`;

fs.writeFileSync(out, header + out_lines.join('\n'), 'utf8');
console.log('已导出 → ' + out);
console.log(`用户 ${stat.user} 条 · 助手 ${stat.assistant} 条 · 工具调用 ${stat.tool} 次`);
console.log('文件大小: ' + (fs.statSync(out).size / 1024).toFixed(1) + ' KB');
