#!/usr/bin/env python3
"""用 Ornn8/memory-system-benchmark 的公开示例卷(108题，MIT)测 MemoryOS：真实 server_new.py + deepseek-chat + bge-m3。
按接入协议逐轮喂历史→每题 retrieve_memory 召回(BENCH_RECALL=v1 复刻原始召回，v2 复刻 hagemi/client/memory-client.ts 的改进召回)→deepseek 作答→按 rubric 判分。
只用公开虚构示例、隔离数据目录；不作弊(作答只看召回，不塞全历史/不喂答案)。"""
import json, subprocess, sys, time, threading, re, os, tempfile, shutil
from datetime import datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
# 考卷：Ornn8/memory-system-benchmark 的公开示例卷（MIT），https://github.com/Ornn8/memory-system-benchmark ，不随本仓库分发
DEMO = os.environ.get('MSB_DEMO_DIR', os.path.join(HERE, 'memory-system-benchmark', 'demo'))
MOS_DIR = os.path.join(REPO, 'memoryos-mcp')
VENV_PY = os.environ.get('MEMORYOS_PYTHON', sys.executable)
RUNTIME_CFG = os.environ.get('MEMORYOS_CONFIG', os.path.join(REPO, 'runtime_config.json'))  # 由 run_memoryos_mcp.sh 生成
RECALL = os.environ.get('BENCH_RECALL', 'v2')  # v1 = 原始召回（3 页 + 3 条知识）；v2 = 改进后的召回
OUT = os.environ.get('BENCH_OUT', os.path.join(HERE, 'out-' + RECALL))
os.makedirs(OUT, exist_ok=True)
def log(*a):
    msg = ' '.join(str(x) for x in a)
    print(msg, flush=True)
    open(OUT + '/progress.log', 'a').write(msg + '\n')

cfg = json.load(open(RUNTIME_CFG))               # 线上配置(key/model/bge-m3/容量)
KEY, BASE, LLM = cfg['openai_api_key'], cfg.get('openai_base_url'), cfg.get('llm_model', 'deepseek-chat')
data_dir = tempfile.mkdtemp(prefix='membench-')
tmpcfg = dict(cfg); tmpcfg['data_storage_path'] = data_dir
tcfg_path = OUT + '/cfg.json'; json.dump(tmpcfg, open(tcfg_path, 'w'))
log('MemoryOS config: model=%s embed=%s short=%s mid_heat=%s retrieval_q=%s' % (
    LLM, cfg.get('embedding_model_name'), cfg.get('short_term_capacity'),
    cfg.get('mid_term_heat_threshold'), cfg.get('retrieval_queue_capacity')))

# ---- 启动真实 MemoryOS MCP server，走 JSON-RPC（同机器人路径）----
srv = subprocess.Popen([VENV_PY, 'server_new.py', '--config', tcfg_path], cwd=MOS_DIR,
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=open(OUT + '/server.stderr.log', 'w'), text=True, bufsize=1)
_pending = {}; _lock = threading.Lock(); _nid = [0]
def reader():
    for line in srv.stdout:
        line = line.strip()
        if not line: continue
        try: m = json.loads(line)
        except: continue
        i = m.get('id')
        if i is not None:
            with _lock:
                ev = _pending.pop(i, None)
            if ev: ev[0] = m; ev[1].set()
threading.Thread(target=reader, daemon=True).start()
def rpc(method, params, timeout):
    with _lock:
        _nid[0] += 1; i = _nid[0]; ev = [None, threading.Event()]; _pending[i] = ev
    srv.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params}) + '\n'); srv.stdin.flush()
    if not ev[1].wait(timeout): raise TimeoutError(method)
    return ev[0]
def tool(name, args, timeout=240):
    r = rpc('tools/call', {'name': name, 'arguments': args}, timeout)
    txt = (((r or {}).get('result') or {}).get('content') or [{}])[0].get('text', '{}')
    try: return json.loads(txt)
    except: return {'status': 'error', 'raw': txt[:200]}

rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'bench', 'version': '1'}}, 120)
srv.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n'); srv.stdin.flush()
log('server ready')

# ---- deepseek 作答/判分 ----
from openai import OpenAI
oai = OpenAI(api_key=KEY, base_url=BASE)
def chat(sys_p, user_p, temp=0.0):
    for _ in range(3):
        try:
            r = oai.chat.completions.create(model=LLM, temperature=temp,
                messages=[{'role': 'system', 'content': sys_p}, {'role': 'user', 'content': user_p}])
            return r.choices[0].message.content.strip()
        except Exception as e:
            log('  deepseek retry:', str(e)[:120]); time.sleep(5)
    return ''

UID = 'PUB01'
def iso2ts(s): return datetime.fromisoformat(s).strftime('%Y-%m-%d %H:%M:%S')

# 复刻 MemoryClient.recall：profile + 最多3条知识 + 最多3页原文
def recall_v1(query):
    r = tool('retrieve_memory', {'user_id': UID, 'query': query, 'max_results': 3}, 120)
    if not r or r.get('status') == 'error': return ''
    lines = []
    prof = (r.get('user_profile') or '').strip()
    if prof and not re.match(r'^no detailed user profile', prof, re.I): lines.append('画像：' + prof[:400])
    for k in (r.get('retrieved_user_knowledge') or [])[:3]:
        lines.append('已知：' + str(k.get('knowledge', k))[:200])
    for p in (r.get('retrieved_pages') or [])[:3]:
        lines.append('往事：用户说「%s」→你答「%s」' % (str(p.get('user_input'))[:160], str(p.get('agent_response'))[:160]))
    return '\n'.join(lines)

# 新版 MemoryClient.recall（v2）：近期原话 + 相关往事(按时间) + 用户知识 + 助手说过的 + 画像，标注主体与日期
def recall(query):
    SHORT_N, PAGE_N, KNOW_N = 10, 7, 4
    r = tool('retrieve_memory', {'user_id': UID, 'query': query, 'max_results': PAGE_N}, 120)
    if not r or r.get('status') != 'success': return ''
    clip = lambda s, n: re.sub(r'\s+', ' ', str(s if s is not None else '')).strip()[:n]
    day = lambda ts: clip(ts, 16)
    pair = lambda p: '[%s] 用户说「%s」→ 你答「%s」' % (day(p.get('timestamp')), clip(p.get('user_input'), 160), clip(p.get('agent_response'), 160))
    sec = []
    recent = (r.get('short_term_memory') or [])[-SHORT_N:]
    rk = set('%s|%s' % (clip(p.get('user_input'), 60), clip(p.get('timestamp'), 19)) for p in recent)
    if recent: sec.append('近期原话（按时间）：\n' + '\n'.join(pair(p) for p in recent))
    pages = [p for p in (r.get('retrieved_pages') or []) if '%s|%s' % (clip(p.get('user_input'), 60), clip(p.get('timestamp'), 19)) not in rk]
    pages = sorted(pages, key=lambda p: str(p.get('timestamp') or ''))[:PAGE_N]
    if pages: sec.append('相关往事（按时间）：\n' + '\n'.join(pair(p) for p in pages))
    uk = ['- [%s] %s' % (day(k.get('timestamp')), clip(k.get('knowledge'), 200)) for k in (r.get('retrieved_user_knowledge') or [])[:KNOW_N]]
    if uk: sec.append('关于用户（长期整理）：\n' + '\n'.join(uk))
    ak = ['- [%s] %s' % (day(k.get('timestamp')), clip(k.get('knowledge'), 200)) for k in (r.get('retrieved_assistant_knowledge') or [])[:KNOW_N]]
    if ak: sec.append('你自己说过/做过的（是你说的，不是用户说的）：\n' + '\n'.join(ak))
    prof = (r.get('user_profile') or '').strip()
    if prof and not re.match(r'^no detailed user profile', prof, re.I): sec.append('画像：' + clip(prof, 400))
    if not sec: return ''
    sec.append('提示：同一件事前后说法不同时，以日期最近的为准；记忆里没有的就说不记得，不要编。')
    return '\n\n'.join(sec)

ANS_SYS = '你是对话助手。只能依据【记忆】里的内容回答用户的问题；记忆里没有的信息就直说不知道/没印象，绝不编造日期、数字、原因或结果。回答简短、直接。'
def answer(q, ctx):
    return chat(ANS_SYS, '【记忆】\n%s\n\n【用户的问题】%s' % (ctx or '(没有检索到相关记忆)', q))

JUDGE_SYS = ('你是严格的阅卷员。判断【系统回答】是否通过。通过条件：满足全部【必需】要点(语义等价即可，不要求逐字)，'
             '且不包含任何【禁止】内容；对【未知】项，如果系统回答把它当成已知事实编了出来即判不通过(但回答"不知道/没提"是对的)。'
             '只看事实/主体/时间/边界，不评语气。只输出 JSON：{"pass": true/false, "why": "一句话"}。')
def judge(q, ans, rub):
    u = '【问题】%s\n【必需】%s\n【可接受表述示例】%s\n【未知(不可编)】%s\n【禁止】%s\n【参考答案(仅示例)】%s\n【系统回答】%s' % (
        q, rub.get('required'), rub.get('acceptable'), rub.get('unknown'), rub.get('forbidden'),
        rub.get('reference_answer', ''), ans)
    out = chat(JUDGE_SYS, u)
    m = re.search(r'\{.*\}', out, re.S)
    try: return bool(json.loads(m.group(0)).get('pass')), out[:160]
    except: return False, out[:160]

# ---- 载入卷子 ----
def jsonl(p): return [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]
questions = jsonl(DEMO + '/exam-inputs.jsonl')
history = jsonl(DEMO + '/histories/PUB01.jsonl')
akey = {a['id']: a for a in jsonl(DEMO + '/review-only/answer-key.jsonl')}
questions.sort(key=lambda q: q['cutoff_turn'])
log('questions=%d history_turns=%d' % (len(questions), len(history)))

results = []; fed = 0; t0 = time.time()
for n, q in enumerate(questions, 1):
    while fed < q['cutoff_turn']:
        t = history[fed]
        tool('add_memory', {'user_id': UID, 'user_input': t['用户消息'], 'agent_response': t['助手回复'],
                            'timestamp': iso2ts(t['逻辑时间'])}, 300)
        fed += 1
        if fed % 40 == 0: log('  fed %d/%d turns (%.0fs)' % (fed, len(history), time.time() - t0))
    ak = akey[q['id']]; rub = {**ak.get('rubric', {}), 'reference_answer': ak.get('reference_answer', '')}
    ctx = (recall_v1 if RECALL == 'v1' else recall)(q['question'])
    ans = answer(q['question'], ctx)
    ok, why = judge(q['question'], ans, rub)
    results.append({'id': q['id'], 'topic': ak.get('content_tags', [None])[0], 'days': ak.get('interval_days'),
                    'pass': ok, 'q': q['question'], 'answer': ans, 'ctx_empty': not ctx, 'why': why})
    if n % 10 == 0 or n == len(questions):
        log('  judged %d/%d  running pass=%d' % (n, len(questions), sum(r['pass'] for r in results)))

# ---- 汇总 ----
P = sum(r['pass'] for r in results); T = len(results)
by_day = defaultdict(lambda: [0, 0]); by_topic = defaultdict(lambda: [0, 0])
for r in results:
    by_day[r['days']][0] += r['pass']; by_day[r['days']][1] += 1
    by_topic[r['topic']][0] += r['pass']; by_topic[r['topic']][1] += 1
summary = {'overall': {'passed': P, 'total': T, 'pct': round(100 * P / T, 1)},
           'by_time_bucket_days': {str(d): {'passed': v[0], 'total': v[1]} for d, v in sorted(by_day.items())},
           'by_topic': {k: {'passed': v[0], 'total': v[1]} for k, v in sorted(by_topic.items(), key=lambda x: x[1][0] / x[1][1])},
           'empty_recall_count': sum(1 for r in results if r['ctx_empty'])}
json.dump({'summary': summary, 'results': results}, open(OUT + '/result.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
log('\n===== 结果 =====')
log('总分：%d / %d  = %.1f%%' % (P, T, 100 * P / T))
log('按时间档：' + json.dumps(summary['by_time_bucket_days'], ensure_ascii=False))
log('召回为空的题数：%d / %d' % (summary['empty_recall_count'], T))
log('最差的几个考点：' + json.dumps(dict(list(summary['by_topic'].items())[:8]), ensure_ascii=False))
try: srv.kill()
except: pass
shutil.rmtree(data_dir, ignore_errors=True)
log('done in %.0fs' % (time.time() - t0))
