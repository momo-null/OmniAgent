# -*- coding: utf-8 -*-
"""库外点歌获取管线（在线曲库，plan §5 / 拍板 §10.2-step4）。

流程：搜（源插件）→ 下（源插件）→ 分离（PyMSS 子进程）→ 切副歌 / 定 key（sing worker）
→ 注册 manifest → 由调用方（msg_sing）转换发送。站点差异全在源插件里（本地不入仓）。

护栏（拍板项）：
- 总闸 ``sing_online``（默认关）——关 = 库外点歌维持"这首我还不会唱"；
- **single-flight**：同 query 全链进行中，二次请求抛 ``InFlightError``（msg_sing 回"在唱了"）；
- 分离全局单飞（``_SEP_LOCK``）：分钟级 GPU 重活同一时刻只跑一个，其余排队等锁；
- manifest 读改写带锁；任何一步失败抛 ``AcquireError``（带阶段），调用方负责"失败也说话"。

本模块不做 GPU 推理：切副歌 / 定 key 走常驻 sing worker（串行锁天然防并发），
分离走独立 PyMSS 子进程（跑完即杀，plan §3.3 两阶段拆分）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict

from utils import get_logger

from plugins.voice import song_source

logger = get_logger("voice.sing_acquire")

_SING_DIR = os.path.join(os.path.expanduser("~"), ".omniagent", "models", "sing")
_MANIFEST_PATH = os.path.join(_SING_DIR, "songs", "manifest.json")
#: 声库舒适带（素材音域实测 p5/p95 加余量，plan §9 step 1）
_BAND = (210.0, 490.0)
#: 完整曲目时长窗（秒）：plan §3.3"时长像完整独唱"，**对下载后的真实时长执行**
#: （搜索页给的时长是站点元数据，可能缺/错；下载完 ffprobe 一测才是事实）
_MIN_DUR, _MAX_DUR = 150.0, 420.0
#: 首个候选被硬筛打掉时最多再试几个（别让群友等无限重试）
_MAX_ATTEMPTS = 2

_MANIFEST_LOCK = threading.Lock()
_SEP_LOCK = threading.Lock()  # 分离全局单飞
_INFLIGHT: Dict[str, float] = {}
_INFLIGHT_LOCK = threading.Lock()


class AcquireError(RuntimeError):
    """获取失败（str = 阶段 + 原因；调用方据此说人话）。"""

    def __init__(self, stage: str, reason: str):
        super().__init__(f"[{stage}] {reason}")
        self.stage = stage


class InFlightError(AcquireError):
    def __init__(self, query: str):
        super().__init__("in-flight", f"同歌获取进行中: {query}")
        self.query = query


def _rvc_python() -> str:
    from plugins.voice.sing_runtime import _sing_python

    return _sing_python()


def _rvc_repo() -> str:
    from plugins.voice.sing_runtime import _sing_repo_dir

    return _sing_repo_dir()


def _pymss_model_dir() -> str:
    from plugins.voice.sing_runtime import voice_cfg

    return str(voice_cfg().get("sing_pymss_model_dir")
               or os.path.join(os.path.expanduser("~"), ".omniagent", "models", "pymss"))


def _normalize_query(query: str) -> str:
    return "".join(str(query or "").split()).lower()


#: 站点标题里的歌名多半在《》里；括号标签（【4K60FPS】/【Hi-Res】）逐层剥掉
_ANGLE = re.compile(r"《([^》]{1,30})》")
_TAG = re.compile(r"^\s*[\[【(（]([^\]】)）]{0,24})[\]】)）]\s*")
_TAIL = re.compile(r"[\s\-–—|，,。!！?？:：·].*$")


def _display_title(raw: str, query: str) -> str:
    """站点标题 → 能对群友说出口的歌名。

    ``msg_sing`` 把条目的 ``title`` 作为 ``song`` 交给模型组织话术，所以这里存的
    必须是"歌名"而不是 B 站 UP 主的 SEO 长标题——"【4K60FPS】陈奕迅《孤勇者》燃到深情!
    谁说站在光里的才算英雄！"整句念出去就不是拟人，是刷屏。原标题另存 ``raw_title``
    留作溯源与别名匹配。
    """
    t = str(raw or "").strip()
    m = _ANGLE.search(t)
    if m:
        return m.group(1).strip()
    prev = ""
    while t and t != prev:
        prev = t
        t = _TAG.sub("", t, count=1).strip()
    t = _TAIL.sub("", t).strip() if t else ""
    return (t or str(query or "").strip())[:24]


def _read_manifest() -> Dict[str, Any]:
    if not os.path.isfile(_MANIFEST_PATH):
        return {}
    try:
        with open(_MANIFEST_PATH, encoding="utf8") as f:
            data = json.load(f)
        return data.get("songs") if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 坏 manifest 视为空，注册时整体重建
        return {}


def _write_manifest(songs: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_MANIFEST_PATH), exist_ok=True)
    with open(_MANIFEST_PATH, "w", encoding="utf8") as f:
        json.dump({"songs": songs}, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _separate(audio_path: str, song_id: str) -> str:
    """PyMSS 分离出人声茎（子进程，跑完即杀；全局单飞）。"""
    out_dir = os.path.join(_SING_DIR, "stems", "acq_out", song_id)
    stem = os.path.join(_SING_DIR, "stems", f"{song_id}_vocals.wav")
    if os.path.isfile(stem):
        return stem
    with _SEP_LOCK:  # 分钟级重活全局单飞；后来者排队（msg_sing 线程等锁=排队语义）
        if os.path.isfile(stem):  # 等锁期间别人做完了
            return stem
        cmd = [
            _rvc_python(), "-m", "tools.pymss.cli", "infer",
            "model_bs_roformer_ep_317_sdr_12.9755",
            "--model-dir", _pymss_model_dir(),
            "-i", audio_path, "-o", out_dir,
            "--device", "cuda", "--format", "wav",
        ]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                           encoding="utf-8", errors="replace", cwd=_rvc_repo())
        if r.returncode != 0:
            raise AcquireError("分离", (r.stderr or r.stdout or "")[-200:])
        outs = []
        for root, _dirs, files in os.walk(out_dir):
            for fn in files:
                if fn.lower().endswith(".wav"):
                    outs.append(os.path.join(root, fn))
        vocals = [p for p in outs
                  if not any(k in os.path.basename(p).lower()
                             for k in ("instrument", "no_vocal", "music", "accompan"))]
        if not vocals:
            raise AcquireError("分离", "未找到人声茎输出")
        os.replace(sorted(vocals)[0], stem)
        logger.info("曲库获取 %s 分离完成（%.0fs）", song_id, time.time() - t0)
        return stem


def _worker_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """经常驻 sing worker 执行 cut_chorus / f0_key（串行锁在 worker 侧）。

    走 ``sing_runtime`` 而**不是** ``plugins.voice.plugin``：loader 按文件路径把
    plugin.py 装载成 `_omni_plugin_voice`，按包名再 import 一次会 exec 出第二份
    模块、第二个 worker 单例 ⇒ 两个 RVC 子进程抢 GPU。运行时状态只住 sing_runtime。
    """
    from plugins.voice import sing_runtime

    fn = {"cut_chorus": sing_runtime.sing_cut_chorus,
          "f0_key": sing_runtime.sing_f0_key}[payload["action"]]
    return fn(payload["input"], **payload.get("kwargs", {}))


def _media_duration(path: str):
    """ffprobe 实测音频时长（秒）；测不了返回 None（**不拿 0 冒充"时长 0 秒"**）。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path or not os.path.isfile(path):
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        return float((r.stdout or "").strip())
    except Exception:  # noqa: BLE001 —— 没 ffprobe / 坏文件：交给调用方按"未筛"处理
        return None


def _stem_duration(path: str):
    """干声 wav 的时长（stdlib wave，**不依赖 PATH**）。

    网关是桌面端拉起的，环境里未必有 ffprobe；没 ffprobe 不等于不筛时长——
    分离产物就是 wav，这里补同一把尺子，量不到仍返回 None（不判死）。
    """
    try:
        import wave

        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            return round(w.getnframes() / rate, 1) if rate else None
    except Exception:  # noqa: BLE001
        return None


def acquire(query: str) -> Dict[str, Any]:
    """歌名 → 曲库条目（已注册 manifest）。失败抛 AcquireError / InFlightError。"""
    if not song_source.online_enabled():
        raise AcquireError("总闸", "在线曲库未开启（sing_online=false）")
    q = _normalize_query(query)
    if not q:
        raise AcquireError("入参", "歌名为空")
    with _INFLIGHT_LOCK:
        if q in _INFLIGHT:
            raise InFlightError(query)
        _INFLIGHT[q] = time.time()
    try:
        return _acquire_locked(query, q)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(q, None)


def _acquire_locked(query: str, q: str) -> Dict[str, Any]:
    sources = song_source.load_sources()
    if not sources:
        raise AcquireError("源", "没有可用的曲库源插件（本地目录为空或全部加载失败）")

    # 1. 搜：第一个 search 成功的源胜出（源自己负责结果质量/排序）
    stage, last_err = "搜索", ""
    candidates = []
    used_source = None
    for sid, mod in sources.items():
        try:
            candidates = list(mod.search(query, limit=3))
            used_source = sid
            break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("源 %s 搜索失败: %s", sid, last_err)
    if not candidates:
        raise AcquireError(stage, f"各源均未命中（{last_err[:120]}）")

    # 2. 下 + 硬筛（§3.3"时长像完整曲目"）：**下载完实测**才判——源给的时长只是先验
    #    （有的源 flat 搜索根本不给，实测 B站 API 给的是页面时长而非音频流时长），
    #    拿不到不等于 0 秒，不许据此把整首歌否掉；实测超窗的（片段/合集/Live 长版）
    #    才换下一个候选。
    stage = "下载"
    picked = False
    cand = None
    audio_path = None
    song_id = ""
    raw_dir = ""
    measured = None
    tried = []
    for cand in candidates[:_MAX_ATTEMPTS]:
        tried.append(cand)
        audio_path = None
        measured = None
        song_id = (f"{used_source}_"
                   + hashlib.sha1(str(cand["id"]).encode("utf-8")).hexdigest()[:10])
        raw_dir = os.path.join(_SING_DIR, "stems", "raw", song_id)
        try:
            audio_path = sources[used_source].fetch(cand, raw_dir)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("曲库获取 %s 下载失败: %s", song_id, last_err)
            continue
        if not audio_path or not os.path.isfile(audio_path):
            last_err = "源未落地音频文件"
            continue
        measured = _media_duration(audio_path)
        if measured is not None and not (_MIN_DUR <= measured <= _MAX_DUR):
            last_err = f"实测时长 {measured:.0f}s 不在 {int(_MIN_DUR)}–{int(_MAX_DUR)}s"
            logger.info("曲库获取 %s 被时长硬筛退回（%s）", song_id, last_err)
            continue
        picked = True
        break
    if not picked:
        raise AcquireError(stage, f"{len(tried)} 个候选都没有 2.5–7min 的完整音轨（{last_err[:120]}）")

    # 3. 分离（全局单飞）
    stage = "分离"
    stem = _separate(audio_path, song_id)
    if measured is None:  # 没 ffprobe 不等于不筛：干声就是 wav，这里补量同一把尺子
        measured = _stem_duration(stem)
        if measured is not None and not (_MIN_DUR <= measured <= _MAX_DUR):
            raise AcquireError("时长筛", f"干声实测 {measured:.0f}s 不在 "
                                        f"{int(_MIN_DUR)}–{int(_MAX_DUR)}s（疑似片段/合集）")

    # 4. 切副歌 + 定 key（常驻 worker，串行）
    stage = "切副歌"
    chorus = os.path.join(_SING_DIR, "chorus", f"{song_id}_c50s.wav")
    _worker_action({"action": "cut_chorus", "input": stem,
                    "kwargs": {"out_wav": chorus, "seconds": 50}})
    stage = "定 key"
    key_info = _worker_action({"action": "f0_key", "input": chorus,
                               "kwargs": {"band_lo": _BAND[0], "band_hi": _BAND[1]}})

    # 5. 注册 manifest（读改写带锁；manifest 是曲库唯一权威）
    raw_title = str(cand.get("title") or "").strip()
    title = _display_title(raw_title, query)
    aliases = [a for a in (query.strip(), title, _normalize_query(raw_title)[:20]) if a]
    entry = {
        "title": title,
        "raw_title": raw_title,
        # 群友说的是歌名不是站点标题（"孤勇者" vs "【4K60FPS】陈奕迅《孤勇者》燃到深情!"），
        # 所以点歌话术与原标题都存成别名——查得到才不会同一首练两遍。
        "aliases": sorted(set(aliases)),
        "chorus": chorus,
        "key": int(key_info["key"]),
        "seed": 1234,
        "index_rate": 0.66,
        # §3.3 拟人硬筛三条（独唱 / 中文 / 音域可移调）里，在线路径只有"实测时长像完整
        # 曲目"和"音域→自动定 key"是可判的；语言与独唱没有检测器，**标 unverified
        # 而不是写死 zh/true**——入库质量兜底靠耳朵，不靠假字段。
        "language": "unverified",
        "solo": "unverified",
        "screened": (["duration_measured", "range_auto_key"] if measured is not None
                     else ["range_auto_key"]),
        "duration_s": measured if measured is not None else cand.get("duration_s"),
        "f0": {k: key_info.get(k) for k in ("p5", "p50", "p95", "out_frac", "voiced")},
        "source": f"{used_source}:{cand.get('id')}",  # 本地记录来源；不进公开仓
    }
    with _MANIFEST_LOCK:
        songs = _read_manifest()
        songs[song_id] = entry
        _write_manifest(songs)
    # 原始下载（m4a）用完即删：L1 缓存留的是干声 stem，原文件只占 C 盘
    shutil.rmtree(raw_dir, ignore_errors=True)
    logger.info("曲库获取完成：%s（%s，key=%+d，%.0fs）",
                song_id, entry["title"], entry["key"], measured or 0.0)
    return {"song_id": song_id, **entry, "key_info": key_info}
