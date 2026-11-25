from __future__ import annotations

import math
import re
import time
from functools import lru_cache
from typing import Any, List


# 给宿主看的接口版本。公开方法的签名/返回结构变了才 +1，跟表结构是两件事：
# 换了个内核但数据没动，只该动这个；数据加了列，只该动上面那个。
# v2：新增否认/状态/锚/分类/梦骨架等公开方法，stats() 返回结构补键（向后兼容追加）。
# v3（2026-11-15）：新增人格分区公开方法（add/update/remove/list_persona、
#     persona_text、reseal_persona_core），core_rule 行新增 kind/source/updated_at。
# v4（2026-11-15）：群聊支持公开方法：speakers_of / speaker_memories（说话人
#     反查）、remember 的 speaker 参数、profile_visibility 三件套（写入/
#     查询/设置）、profiles 的 visibility 过滤、activate 的 profile_visibility
#     参数、set_attitude / get_attitude（坏感度）、recent_messages 的
#     session_id 参数。
CORE_API_VERSION = 4

# 可往返导出的文件：格式名 + 文件自身版本（排版变了才 +1）
# v2（R5）：文件头带每表行数清单与总行数，尾部加 __eof__ 标记；
# 导入 v2 一律严格校验（坏行/截断/行数不符报错，落库前先在临时库预演），
# v1 老备份保持宽容兼容（坏行跳过计数）。
# v3（P0-1）：access_log 纳入备份。它虽是体温计不是账本，但近期命中、
# 工作记忆复出、访问审计都吃它的明细，少了它「整本原样搬家」就是谎报。
# 破坏性变更：旧代码（只认 v2）读到 v3 备份会在文件头拒收——
# 「备份格式 v3 比当前支持的 v2 新」；反之 v2 老备份没有 access_log 段，
# 新代码照收，只是访问明细不在里面（这正是当年那个洞）。
# v4（2026-11-15）：creative 表纳入备份（schema v6）。同 access_log 先例：
# 升本的搬家不带创作项目就是丢数据；v4 备份旧代码拒收，v3 及以下照收。
# v5（2026-11-15）：core_rule 的 kind/source/updated_at 三列随备份走
# （schema v7）。人格分区是「这个人是谁」的基础设定，搬家丢了它，
# 新库上的人格就不完整。破坏性变更同 v4 先例：v5 备份旧代码在文件头
# 拒收（「备份格式 v5 比当前支持的 v4 新」）；v4 及以下老备份没有三列，
# 新代码照收，列落默认值（kind='self'）。
# v6（2026-11-15）：群聊支持三张表随备份走（schema v8）。说话人丢了
# 「谁说的」就没法反查，画像可见性丢了注入口径会翻车，坏感度丢了
# 分级白记；v6 备份旧代码在文件头拒收，v5 及以下老备份没有三张表，
# 新代码照收——替换式恢复时本地这三张表原样保留（照 creative 先例）。
DUMP_FORMAT = "jj-memory-dump"
DUMP_VERSION = 6

# 搬家时要带走的表，顺序按「先元信息、后投影」，导入时不会因为顺序互相绊住
DUMP_TABLES = ("meta", "mcommit", "commit_row", "memory", "clue",
               "memory_clue", "memory_link", "profile", "core_rule",
               "message",
               # v5：职守状态与梦。这些不是体温计，是「判决与证据」——
               # 状态档丢了会冤枉记忆，否认丢了会放跑幻觉，锚丢了断回原文的路。
               "memory_condition", "memory_status", "dream_proposal",
               "memory_source_anchor", "user_denial",
                # v3（P0-1）：access_log 是运行状态不是事实，但近期命中与
                # 工作记忆复出按时间窗口吃它的明细，备份丢了它，迁移前后
                # 的行为就对不上——体温计的读数历史也是要搬家的。
                "access_log",
                # v4/v6（2026-11-15）：creative 是创作项目正文与原始档，
                # 搬家丢表就是丢作品，必须带走。
                "creative",
                # v8（2026-11-15）：群聊支持三张表。说话人/可见性都在提交链上，
                # 坏感度是手动设置的用户数据；搬家丢表=丢「谁说的、什么口径、
                # 什么态度」，必须带走。
                "memory_speaker", "profile_visibility", "user_attitude")

# v8 群聊三表的表名清单：备份兼容判断用（旧备份不带这三张表时，
# 替换式恢复不许把它们清掉）
V8_DUMP_TABLES = ("memory_speaker", "profile_visibility", "user_attitude")

# 每张表导出时的稳定排序，纯粹为了 diff 好看，往返不依赖顺序
_DUMP_ORDER = {
    "meta": "k",
    "mcommit": "id",
    "commit_row": "commit_id, seq",
    "memory": "created_seq, id",
    "clue": "id",
    "memory_clue": "memory_id, clue_id",
    "memory_link": "id",
    "profile": "user_id, id",
    "core_rule": "id",
    "message": "seq",
    "memory_condition": "memory_id, cond_json",
    "memory_status": "id",
    "dream_proposal": "created_at, id",
    "memory_source_anchor": "memory_id, message_seq",
    "user_denial": "id",
    "access_log": "id",
    "creative": "created_at, id",
    "memory_speaker": "memory_id, speaker_id",
    "profile_visibility": "profile_id",
    "user_attitude": "user_id",
}

# meta 里属于「这台机器此刻的状态」而不属于「账本内容」的键，导出时跳过
_LOCAL_META = frozenset({"last_export_date", "health_probe"})

LEVELS = ("topic", "day", "week", "month", "quarter", "year")
GRADES = {0: "零级", 1: "一级", 2: "二级"}
MAX_GRADE = 2

# 自动沉淀条目的机器可读类型标记（N1 降级）。这类条目是「聊过什么」的原文
# 索引：正文只是前 60 字摘录 + 词面碎片关键词，当普通事实注入会把索引抬成
# 结论。统一标签「自动沉淀」保留给人看和显式检索；唤起（activate）各条路线
# 按 auto_sink_ids() 识别这个标记并一律排除，普通记忆注入路径不再收它们。
AUTO_SINK_TYPE_TAG = "type:auto_sink"

# 自动沉淀条目的统一标签（N2）。main.AUTO_SINK_TAG 与这里必须同一字面。
# 两个标记进 tags 都只为「识别 + 显式检索」（auto_sink_ids() 精确匹配
# type:auto_sink；recall 搜「自动沉淀」靠 tags 命中），不是为了当概念。
AUTO_SINK_LABEL = "自动沉淀"

# 自动沉淀的完整机器标记集合（N6）：识别口径与建图口径（is_machine_tag）
# 同源——tags 里带其中任一个完整标签就算沉淀。1.5.0 期间写出的存量
# 条目只有「自动沉淀」标签，靠这里兜住；打开旧库时另有
# _migrate_auto_sink_legacy() 补上类型标记。
AUTO_SINK_MARKERS = frozenset({AUTO_SINK_TYPE_TAG, AUTO_SINK_LABEL})


def is_machine_tag(tag: str) -> bool:
    """机器/管理标签判定（N2）：type: 前缀的类型标记，或自动沉淀的统一标签。

    tags 即概念、写入即建图（_auto_link），所以「识别用的标记」一旦写进
    tags，就会被当成语义概念和历史条目织网——实测 5 条沉淀探针自己连出
    20 条 concept 边（「自动沉淀」与 type:auto_sink 各 10 条），把「聊过
    什么」的原文索引抬成了图谱上的语义同源。这类标签在唯一建图入口
    _auto_link 里统一拦下：它们不配当 concept，也不把别的条目和沉淀
    索引互相连线；检索、识别、注入排除一概不受影响。
    """
    t = (tag or "").strip()
    return t.startswith("type:") or t == AUTO_SINK_LABEL


CLUE_SORT_BOOST = 0.6  # 线索权重对主排序的放大上限（稀有线索最多 ×1.6）
CLUE_ONLY_BASE = 0.35  # 只有线索撞上、词面没命中时的兜底分


DEFAULT_INJECT_SLOTS = 3     # 每轮最多往提示词里放几条（「说出口」的上限）。
                             # 别和 WORKING_CAPACITY（工作记忆能坐几人）混：
                             # 一个是这一轮说几句，一个是长期留几个位置。


WORKING_DEMOTE_LIMIT = 20       # 一次最多挤掉几条：分期收敛，提交链上不留一坨两百行的快照
REVIVE_WINDOW_DAYS = 3.0        # 零级记忆在这个时间窗里被想起……
REVIVE_HITS = 2                 # ……这么多次，就让它回到工作记忆
ZERO_GRADE_WEIGHT = 0.45        # 零级在自动激活里的分量：不主动冒头，但没被拉黑
WORKING_COOLDOWN_SECONDS = 6 * 3600.0  # maybe_rebalance 最短多久真跑一次


WAL_CHECKPOINT_COOLDOWN = 300.0  # 两次 checkpoint 之间至少隔 5 分钟


# 坏感度表里 owner 的身分（v8）：画像层对主人的 user_id 固定用这个字面
# （main.memory_profile 写死），坏感度写路径遇到它一律拒绝——owner 的
# 等级严禁修改，门面层与 builder 层都拦，没有旁路。
ATTITUDE_OWNER_ID = "master"

# 画像可见性（v8）的合法取值。缺行按 private 处理（保守），只在显式
# 说 public 时才对群聊可见。
PROFILE_VISIBILITIES = ("private", "public")

# 人格分区（v7）：core_rule.kind 的合法取值。
# self = 可编辑人格设定（add_persona 默认分区）；core = 只读基础设定，
# 只能由 _seed_persona_core 显式播种，update/remove 一律拒绝。
PERSONA_KINDS = ("self", "core")

# 哈希哨兵（v7）：meta 键名。kind='core' 的行按 id 排序后
# sha256(拼 id + \x1f + content + \x1f + priority + \x1e)，
# 封链/播种时自动重算；启动与 health() 各核一次，不符只报警记日志，
# 不阻止启动、不自动修，人工确认后 reseal_persona_core 重封。
PERSONA_CORE_HASH_KEY = "persona_core_hash"


# 压缩后用来接管历史的基线提交（序号 0，永远排在最前）
BASELINE_COMMIT = "C0"


# 状态全集（v5 §3.3）：新增第四档 coexist（条件并存）。
# 三档旧状态落不下「明天热的时候还提『喝热的』」——那是不合时宜，不是记错。
MEMORY_STATUSES = ("active", "coexist", "conflicted", "superseded",
                   "corrected", "disputed")

# disputed 是粘性状态：用户否认写进去之后，自动写入不得覆盖（见 _latest_status_map）。
# 解除只有一条路：deny_revoke() 显式写入 reason 以 DENY_REVOKE_PREFIX 开头的记录。
DENY_STICKY_STATUS = "disputed"
DENY_REVOKE_PREFIX = "用户撤销否认"

# 用户否认关键词（v5 §5.1）：命中只进待确认队列，不自动改状态。
# 「老大说一句『你记错了』」走 deny_memory 工具（显式指定 memory_id），
# 权重最高、立刻生效、不等梦跑；关键词命中只是线索，不是判决。
DENIAL_KEYWORDS = ("记错了", "不是这样", "没这回事", "我没说过")

# 矛盾四分类（v5 §3.2）：这是分类，不是判决。前三类大多是真的（合法矛盾），
# 只有第四类才进幻觉候选，而且还要过原文对账。
CONFLICT_TEMPORAL = "temporal"          # 时变：冷天喝热的 / 热天喝冷的
CONFLICT_SCENE = "scene_dependent"      # 场景依赖：公司要安静 / 家里要热闹
CONFLICT_PERSPECTIVE = "perspective"    # 视角差异：同一件事你我记的不一样
CONFLICT_MUTEX = "true_mutex"           # 真互斥候选：还要过原文对账
CONFLICT_UNDETERMINED = "undetermined"  # 条件不足：挂 conflicted 等证据，不许顺手判错

# 词面互斥对：只作真互斥的必要条件之一，不作判决。
# 判据只有一句「原文里根本没这回事」（v5 §2.3），词面打架不算。
MUTEX_PAIRS = (("要", "不要"), ("喜欢", "讨厌"), ("爱吃", "不吃"),
               ("热的", "冷的"), ("开着", "关着"), ("是", "不是"),
               ("在", "不在"), ("会", "不会"), ("有", "没有"))

# 梦的三条硬约束（v5 §2.1），全部硬编码，不可配置调高：
#   置信上限 0.4 / 单次最多产出 2 条 / 永不进 no_cold 白名单。
# no_cold 机制本身尚未实现（v3 设计），约束以结构落地：importance ≤ 0.4
# 触发不了 ≥0.85 的钉住与年龄豁免，tags 剔除保护标签，level 恒为 day
# （不碰 topic 的冲刷豁免），dream 产出的记忆随时可删、不算证据。
DREAM_CONFIDENCE_CAP = 0.4
DREAM_MAX_OUTPUTS = 2
DREAM_PROVENANCE = "dream"


# 被否认（disputed）的记忆在检索里的降权系数：立刻降权重、立刻停止主动提起，
# 但不删除——否认不等于删，原文还在（v5 §5.1）。
DENIED_RECALL_WEIGHT = 0.3


# memory 表里这俩列不是「记忆内容」，是「记忆被翻过多少次」的缓存。
# 它们不进提交快照：想一次不改变记忆本身，不值得在账本里留一页。
# 真值以 access_log 为准，这两列只是为了让衰减计算不用每次去 COUNT。
STATE_ONLY_COLUMNS = frozenset({"access_count", "last_access_seq"})

# 每张投影表的行主键：回放/压缩时用来认「这是不是同一行」
PROJECTIONS = {
    "memory": lambda r: r.get("id"),
    "clue": lambda r: r.get("id"),
    "memory_clue": lambda r: "%s|%s" % (r.get("memory_id"), r.get("clue_id")),
    "memory_link": lambda r: "%s|%s|%s" % (r.get("from_id"), r.get("to_id"), r.get("concept")),
    "profile": lambda r: r.get("id"),
    "core_rule": lambda r: r.get("id"),
    # v8：群聊支持三张表进提交链，回放/压缩/对账同样要认得它们
    "memory_speaker": lambda r: "%s|%s" % (r.get("memory_id"), r.get("speaker_id")),
    "profile_visibility": lambda r: r.get("profile_id"),
    "user_attitude": lambda r: r.get("user_id"),
}


class LedgerError(RuntimeError):
    """提交链层面的错误。"""


class StaleHeadError(LedgerError):
    """开提交时记下的 HEAD 已经不是当前 HEAD。"""


FLUSH_K = 6.0          # 冲刷压力分母系数
HALF_LIFE_DAYS = 30.0  # 检索的时间半衰期（animaone 用的是 30 天）


MAX_RECALL_LIMIT = 50        # 一次最多唤起多少条，再大就夹住


HALF_LIFE_ACCESS_BOOST = 0.5  # 每被访问一次，半衰期的对数级加成
MAX_HALF_LIFE_FACTOR = 5.0    # 访问再多，半衰期最多拉长到 5 倍


STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "和", "就", "也",
    "都", "又", "还", "这", "那", "有", "没", "不", "很", "吗", "呢", "吧",
    "啊", "呀", "哦", "嗯", "把", "被", "给", "让", "对", "从", "到", "会",
}

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}")
_ASCII_NUM = re.compile(r"\d{2,}")


def now_str() -> str:
    """当前时间的账本字符串：本地时间、无时区偏移（P1-5 已知契约）。

    账本里所有时间戳都是这个格式：写的是哪台机器的本地时间，没有时区
    偏移。跨时区搬库后这些字符串会被新机器按自己的时区解释，时间差、
    每日导出日期、访问窗口都可能偏——这是既有的已知限制，统一改 UTC
    要牵动存量数据的解释，本轮不动它。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@lru_cache(maxsize=8192)
def _parse_time_cached(text: str) -> float:
    """时间字符串 → 时间戳。同一个字符串（记忆的 created_at）只解析一次。

    strptime 一次要几十微秒，两千条记忆每轮要解析几千次，光这一项就吃掉近半时间。
    解析不出来返回 0，让调用方自己决定退回「现在」。

    时区契约（P1-5）：time.mktime 把无偏移的本地时间字符串按「运行机器
    的本地时区」解释，跨时区搬库后同一串时间会算出不同的绝对年龄；
    存量数据都是本地时间，统一改 UTC 是迁移级改动，本轮不动它。
    """
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except Exception:
            continue
    return 0.0


def _parse_time(value: Any, *, _parse_time_cached) -> float:
    """时间值 → 时间戳；int/float 直通，字符串按运行机器本地时区解释（P1-5）。"""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return time.time()
    ts = _parse_time_cached(str(value)[:19])
    return ts if ts else time.time()


# --------------------------------------------------------------------------
# 切词与打分（animaone：单字 + 双字滑窗，无词典）
# --------------------------------------------------------------------------

def human_ago(value: Any, *, _parse_time) -> str:
    """把时间说成人话：刚刚 / 昨天 / 三天前 / 上周。"""
    ts = _parse_time(value)
    delta = max(0.0, time.time() - ts)
    if delta < 3600:
        return "刚刚"
    if delta < 86400:
        return "%d 小时前" % int(delta // 3600)
    if delta < 172800:
        return "昨天"
    if delta < 604800:
        return "%d 天前" % int(delta // 86400)
    if delta < 2592000:
        return "%d 周前" % max(1, int(delta // 604800))
    return "%d 个月前" % max(1, int(delta // 2592000))


def tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    tokens: List[str] = []
    for w in _ASCII_WORD.findall(text):
        tokens.append(w)
    for n in _ASCII_NUM.findall(text):
        tokens.append(n)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            if run not in STOPWORDS:
                tokens.append(run)
            continue
        # 只用双字滑窗：单字命中太吵，「杯子」的「子」会撞上「量子」
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram[0] in STOPWORDS and gram[1] in STOPWORDS:
                continue
            tokens.append(gram)
    return tokens


def _bm25_scores(docs: List[List[str]], query: List[str],
                 k1: float = 1.5, b: float = 0.75) -> List[float]:
    n = len(docs)
    if n == 0 or not query:
        return [0.0] * n
    avgdl = sum(len(d) for d in docs) / max(1, n) or 1.0
    df: Dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    q_terms = set(query)
    scores = [0.0] * n
    for i, d in enumerate(docs):
        if not d:
            continue
        dl = len(d)
        tf: Dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        for term in q_terms:
            f = tf.get(term)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = f + k1 * (1 - b + b * dl / avgdl)
            scores[i] += idf * (f * (k1 + 1)) / denom
    return scores


def normalize_limit(limit: Any, default: int = 5) -> int:
    """把调用方给的 limit 收进 [1, MAX_RECALL_LIMIT]：字符串、0、负数、超大值都不许炸。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(MAX_RECALL_LIMIT, n))


def effective_half_life(base_half_life: float = HALF_LIFE_DAYS,
                        access_count: Any = 0) -> float:
    """被访问过的记忆衰减得更慢：访问次数对数级拉长半衰期，但有上限。

    常被想起的事应该在时间里站得更久；封顶是怕一条热点记忆永远不淡。
    """
    base = max(1e-6, float(base_half_life or HALF_LIFE_DAYS))
    try:
        n = max(0, int(access_count or 0))
    except (TypeError, ValueError):
        n = 0
    factor = 1.0 + math.log2(1.0 + n) * HALF_LIFE_ACCESS_BOOST
    return min(base * factor, base * MAX_HALF_LIFE_FACTOR)


def clue_weight(total_entities: int, use_count: int) -> float:
    """线索的辨识度（ai-memory 的做法）。

    越少记忆共用的线索越值钱：「空调」只在两三条里出现，撞上就准；
    「日常」挂在五十条上，撞上等于没说。归一化 IDF，落在 0.1 ~ 1.0。
    """
    total_entities = max(0, int(total_entities))
    if total_entities <= 1:
        return 1.0
    use_count = max(1, min(total_entities, int(use_count or 0)))
    max_idf = math.log((total_entities + 1) / 2.0)
    if max_idf <= 1e-9:
        return 1.0
    norm = math.log((total_entities + 1) / (use_count + 1)) / max_idf
    return 0.1 + 0.9 * max(0.0, min(1.0, norm))


def retention_index(importance: float, clue_count: int, access_count: int) -> float:
    """保留指数：对数级巩固。重要性 0.5 + 两线索 + 三次访问 ≈ 3.9。"""
    importance = max(0.01, min(1.0, float(importance or 0.0)))
    clue_term = 1.0 + math.log2(1.0 + max(0, clue_count))
    access_term = 1.0 + math.log2(1.0 + max(0, access_count))
    return importance * clue_term * access_term


def flush_pressure(created_seq_now: int, last_access_seq: int, retention: float) -> float:
    """冲刷量 ÷ (保留指数 × K)。冲刷量为 0 时恒为 0。"""
    delta = max(0, int(created_seq_now) - int(last_access_seq))
    if delta == 0:
        return 0.0
    return delta / max(1e-6, retention * FLUSH_K)


def _clamp_unit(value: Any, default: float = 0.5) -> float:
    """把 0~1 的强度收进区间：None / nan / inf / 非数字一律回默认值。

    nan 尤其要拦：Python 里 min(1.0, nan) 返回 1.0，于是「没给值」会被
    悄悄记成满分、钉住不淘汰——方向刚好反了。
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n or math.isinf(n):
        return default
    return max(0.0, min(1.0, n))
