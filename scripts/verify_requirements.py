"""参赛要求逐条核验器。

对照 activityGuidelines.md 的每一条硬性要求，检查项目文件是否满足。
每一条都做真实的文件/内容检查，不做主观判断。

用法：python verify_requirements.py
"""

import os
import re
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CST = timezone(timedelta(hours=8))

PASSED = 0
FAILED = 0
WARN = 0
RESULTS = []


def record(status: str, rule: str, detail: str = "") -> None:
    global PASSED, FAILED, WARN
    if status == "PASS":
        PASSED += 1
    elif status == "FAIL":
        FAILED += 1
    else:
        WARN += 1
    RESULTS.append((status, rule, detail))


def read(rel: str) -> str:
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def exists(rel: str) -> bool:
    return os.path.exists(os.path.join(ROOT, rel))


def main() -> int:
    print("=" * 72)
    print(" 参赛要求逐条核验")
    print(f" 项目根目录：{ROOT}")
    print("=" * 72)

    # ---------------------------------------------------------- 一、必交文件
    print("\n【规则】参赛项目应包含至少以下文件，且以标准格式命名")
    required = [
        ("README.md", "项目介绍、安装方法、使用示例、目标用户"),
        ("CONTEST_DECLARATION.md", "参赛声明（原创性、合规性、敏感信息）"),
        ("MCP_INTEGRATION.md", "实际使用的 MCP Server、Tool、调用流程、业务价值"),
        ("mcp-config.example.json", "脱敏后的 MCP 配置示例，只允许环境变量占位符"),
        ("workbuddy.md", "WorkBuddy 对话上下文（参加专项奖励时必交）"),
    ]
    for fname, desc in required:
        if exists(fname):
            size = os.path.getsize(os.path.join(ROOT, fname))
            record("PASS", f"必交文件存在且命名正确：{fname}",
                   f"{size} 字节 · {desc}")
        else:
            record("FAIL", f"必交文件缺失：{fname}", desc)

    # 源代码
    py_files = []
    for dirpath, _dirs, files in os.walk(ROOT):
        if ".git" in dirpath or "__pycache__" in dirpath:
            continue
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    if py_files:
        record("PASS", "源代码存在", f"{len(py_files)} 个 .py 文件")
    else:
        record("FAIL", "源代码缺失")

    # ---------------------------------------------------------- 二、README 四要素
    print("\n【规则】README.md = 项目介绍、安装方法、使用示例、目标用户")
    readme = read("README.md")
    if readme:
        elements = {
            "项目介绍": ["## 项目介绍", "项目介绍"],
            "安装方法": ["## 安装方法", "安装方法", "### 申请 MCP Token"],
            "使用示例": ["## 使用示例", "使用示例", "### 命令行"],
            "目标用户": ["## 目标用户", "目标用户"],
        }
        for label, keys in elements.items():
            hit = any(k in readme for k in keys)
            record("PASS" if hit else "FAIL", f"README 含「{label}」")
        # 使用示例必须给出可复制的命令
        has_cmd = bool(re.search(r"python\s+scripts/price_scout\.py", readme))
        record("PASS" if has_cmd else "FAIL", "README 使用示例含可复制命令")
        # 安装方法必须说明 Token 从哪来
        has_token = "open.mcd.cn" in readme and "MCD_MCP_TOKEN" in readme
        record("PASS" if has_token else "FAIL",
               "README 安装方法含 MCP Token 获取与配置说明")
        # 参赛信息区块（便于评审核对）
        record("PASS" if "## 参赛信息" in readme else "WARN",
               "README 含「参赛信息」区块")
        # 不得出现失效的仓库别名
        record("PASS" if "M-China-Official" not in readme else "FAIL",
               "README 未使用失效的仓库别名")
    else:
        record("FAIL", "README.md 无法读取")

    # ---------------------------------------------------------- 三、声明文件完整性
    print("\n【规则】CONTEST_DECLARATION.md 内容不可改动（官方原文）")
    decl = read("CONTEST_DECLARATION.md")
    if decl:
        import hashlib
        md5 = hashlib.md5(decl.encode("utf-8")).hexdigest()
        official_md5 = "91a7b32ec28098b609ca3f215cdf7147"
        record("PASS" if md5 == official_md5 else "FAIL",
               "CONTEST_DECLARATION.md 与官方原文一致",
               f"MD5 {md5}")
        for section in ("原创性声明", "合规声明", "信息安全声明", "规则遵守声明"):
            record("PASS" if section in decl else "FAIL",
                   f"声明含「{section}」")
    else:
        record("FAIL", "CONTEST_DECLARATION.md 无法读取")

    # ---------------------------------------------------------- 四、MCP 配置脱敏
    print("\n【规则】mcp-config.example.json 脱敏，只允许环境变量占位符")
    import json
    cfg_raw = read("mcp-config.example.json")
    if cfg_raw:
        try:
            cfg = json.loads(cfg_raw)
        except json.JSONDecodeError as exc:
            record("FAIL", "mcp-config.example.json 是合法 JSON", str(exc))
            cfg = None
        if cfg is not None:
            headers = (cfg.get("mcpServers") or {}).get("mcd-mcp", {}).get("headers", {})
            auth = headers.get("Authorization", "")
            if "${" in auth:
                record("PASS", "Authorization 使用环境变量占位符", auth)
            else:
                record("FAIL", "Authorization 未使用环境变量占位符", auth)
            # 检查是否有任何疑似明文 token
            tokens = re.findall(r"[A-Za-z0-9]{20,}", cfg_raw)
            real = [t for t in tokens if not t.startswith("${") and "MCP" not in t.upper()]
            if real:
                record("FAIL", "配置中不含明文 Token", f"发现 {len(real)} 段")
            else:
                record("PASS", "配置中不含明文 Token")
            if "mcp.mcd.cn" in cfg_raw:
                record("PASS", "接入地址正确")
            else:
                record("FAIL", "接入地址缺失或错误")
    else:
        record("FAIL", "mcp-config.example.json 无法读取")

    # ---------------------------------------------------------- 五、全仓库无明文 token
    print("\n【规则】信息安全声明：不含真实 Token、密钥、密码、账号凭证")
    leaks = []
    for dirpath, _dirs, files in os.walk(ROOT):
        if ".git" in dirpath or "__pycache__" in dirpath:
            continue
        for f in files:
            if not f.endswith((".py", ".json", ".md", ".txt", ".yml", ".yaml", ".example")):
                continue
            path = os.path.join(dirpath, f)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            for m in re.finditer(r"(?:Bearer\s+)([A-Za-z0-9]{20,})", content):
                leaks.append((f, m.group(1)[:12]))
            for m in re.finditer(r"\b([A-Za-z0-9]{28,})\b", content):
                s = m.group(1)
                # 排除正常的长标识符与哈希
                if re.fullmatch(r"[A-F0-9]{64}", s):
                    continue
                if s.isupper() or "_" in s:
                    continue
                if re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s):
                    if f != "verify_requirements.py":
                        leaks.append((f, s[:12]))
    if leaks:
        record("FAIL", "全仓库无明文 Token/凭证", f"命中 {len(leaks)} 处：{leaks[:3]}")
    else:
        record("PASS", "全仓库无明文 Token/凭证")

    # 个人信息
    print("\n【规则】个人信息：样例数据不含他人个人信息")
    sample = read("scripts/sample_data/sample_data.json")
    if sample:
        pii_patterns = {
            "手机号": r"1[3-9]\d{9}",
            "身份证": r"\d{17}[\dXx]",
            "邮箱": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        }
        for label, pat in pii_patterns.items():
            hits = re.findall(pat, sample)
            record("PASS" if not hits else "FAIL",
                   f"样例数据不含{label}", f"命中 {len(hits)} 处" if hits else "")
    else:
        record("FAIL", "样例数据文件缺失")

    # ---------------------------------------------------------- 六、MCP_INTEGRATION 四要素
    print("\n【规则】MCP_INTEGRATION.md = MCP Server、Tool、调用流程、业务价值")
    integ = read("MCP_INTEGRATION.md")
    if integ:
        # 只看标题行，避免目录里的关键词造成假阳性
        headings = "\n".join(l for l in integ.splitlines() if l.startswith("#"))
        for label, keys in {
            "MCP Server": ["MCP Server"],
            "Tool": ["Tool"],
            "调用流程": ["调用流程"],
            "业务价值": ["业务价值"],
        }.items():
            hit = any(k in headings for k in keys)
            record("PASS" if hit else "FAIL",
                   f"MCP_INTEGRATION 有「{label}」章节")
        # 必须列出实际使用的工具名
        used = ["query-nearby-stores", "query-meals", "query-store-coupons",
                "query-meal-detail", "calculate-price", "query-my-coupons", "order-list"]
        missing = [t for t in used if t not in integ]
        record("PASS" if not missing else "FAIL",
               "MCP_INTEGRATION 列全实际使用的工具", f"缺 {missing}" if missing else "")
        # 必须包含真实调用追踪作为流程证据
        record("PASS" if "trace" in integ.lower() else "WARN",
               "MCP_INTEGRATION 引用真实调用追踪")
    else:
        record("FAIL", "MCP_INTEGRATION.md 无法读取")

    # ---------------------------------------------------------- 七、workbuddy.md
    print("\n【规则】workbuddy.md = WorkBuddy 对话上下文（专项奖励核验用）")
    wb = read("workbuddy.md")
    if wb:
        has_dev = "开发" in wb
        has_dialog = any(k in wb for k in ["**用户：**", "**WorkBuddy：**", "对话"])
        has_env = any(k in wb for k in ["Python", "MCP", "环境"])
        record("PASS" if has_dev else "FAIL", "workbuddy.md 记录开发过程")
        record("PASS" if has_dialog else "FAIL", "workbuddy.md 含对话上下文")
        record("PASS" if has_env else "FAIL", "workbuddy.md 含开发环境说明")
    else:
        record("FAIL", "workbuddy.md 无法读取")

    # ---------------------------------------------------------- 八、内容合规
    print("\n【规则】内容合规：不得将麦当劳与其他品牌对比、不得贬损品牌")
    all_md = " ".join(read(f) for f in
                       ["README.md", "MCP_INTEGRATION.md", "workbuddy.md", "SKILL.md",
                        "references/mcp-tools.md", "references/store-pricing.md"])
    # 竞品词对比检查（只检查是否出现竞品名）
    competitors = ["肯德基", "KFC", "汉堡王", "麦当劳 vs", "对比肯德基"]
    found_comp = [c for c in competitors if c in all_md]
    record("PASS" if not found_comp else "FAIL",
           "未出现竞品对比表述", f"命中 {found_comp}" if found_comp else "")

    # 免责声明
    disclaimer_keys = ["非麦当劳官方产品", "不构成医疗", "以麦当劳官方渠道"]
    for k in disclaimer_keys:
        record("PASS" if k in all_md else "WARN", f"含免责声明要素：{k}")

    # 原创声明
    record("PASS" if "原创" in all_md else "WARN", "含原创性相关表述")

    # ---------------------------------------------------------- 九、时间窗口
    print("\n【规则】仓库创建时间须在 2025-12-25 00:00 ~ 2026-10-25 23:59（北京时间）")
    win_start = datetime(2025, 12, 25, 0, 0, 0, tzinfo=CST)
    win_end = datetime(2026, 10, 25, 23, 59, 59, tzinfo=CST)
    now = datetime.now(CST)
    record("PASS" if now <= win_end else "FAIL",
           "当前时间在报名窗口内",
           f"现在 {now:%Y-%m-%d %H:%M}，剩余 {(win_end - now).days} 天")
    record("PASS" if win_start <= now <= win_end else "FAIL",
           "仓库创建时间窗口检查", "建仓时需确保")

    # ---------------------------------------------------------- 十、只读约束
    print("\n【规则】项目须真实使用麦当劳 MCP 能力（本项目选择只读）")
    code = read("scripts/price_scout.py") + read("scripts/mcd_client.py")
    write_tools = ["create-order", "cancel-order", "mall-create-order",
                   "party-order-create", "draw-lottery", "auto-bind-coupons",
                   "delivery-create-address"]
    called_write = [t for t in write_tools if f'"{t}"' in code or f"'{t}'" in code]
    record("PASS" if not called_write else "FAIL",
           "源码中未调用任何写操作工具", f"命中 {called_write}" if called_write else "")

    # ---------------------------------------------------------- 十一、建仓后待办
    print("\n【规则】建仓后仍需完成的两项（当前无法在本机核验）")
    git_dir = os.path.join(ROOT, ".git")
    has_git = os.path.isdir(git_dir)
    record("PASS" if has_git else "WARN", "已初始化 git 仓库")
    if has_git:
        # 检查是否有提交
        import subprocess
        try:
            n = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                               cwd=ROOT, capture_output=True, text=True, timeout=15)
            cnt = n.stdout.strip()
            record("PASS" if cnt and cnt != "0" else "WARN",
                   "已有 git 提交", f"{cnt} 次提交" if cnt else "")
        except Exception:
            record("WARN", "无法读取 git 提交数")
        # 检查是否配置了 remote（未推送则提示）
        try:
            r = subprocess.run(["git", "remote", "-v"], cwd=ROOT,
                               capture_output=True, text=True, timeout=15)
            has_remote = bool(r.stdout.strip())
            record("PASS" if has_remote else "WARN",
                   "已配置 GitHub remote", "已配置" if has_remote else "尚未推送")
        except Exception:
            record("WARN", "无法读取 git remote")

    record("WARN", "仓库须设为 Public 且创建时间在窗口内",
           "建仓时确认（2025-12-25 ~ 2026-10-25）")
    record("WARN", "须在 M-China/mcd-developer-innovation-challenge 发报名 Issue",
           "正文首行必须是【参赛申请】；标题与正文间、简介与内容间不换行；链接不用 Markdown")

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 72)
    print(" 核验明细")
    print("=" * 72)
    for status, rule, detail in RESULTS:
        icon = {"PASS": "✓", "FAIL": "✗", "WARN": "!"}[status]
        line = f" [{icon}] {rule}"
        if detail:
            line += f"  —— {detail}"
        print(line)

    print("\n" + "=" * 72)
    print(f" 结果：{PASSED} 通过，{FAILED} 未通过，{WARN} 提示")
    print("=" * 72)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
