// 语音采集引擎：按住说话（对讲）与免提（对空气说话）共用一条麦克风流。
//
// - AudioWorklet 每 512 帧批一条（~32ms@16k），主线程按块做能量 VAD；
// - 按住说话：订阅期间累积，松手编码 WAV（微信对讲式）；
// - 免提：常开 VAD 自适应静噪地板，静音 ~0.7s 自动判句 → 完整话语回调提交；
//   播报中阈值抬高且需持续 ~240ms 人声才触发「声学打断」（浏览器 AEC 负责压制
//   扬声器回声，打断瞬间再截 250ms 编码头兜底）。
// 零第三方依赖；麦克风由订阅计数管理（无人订阅即释放，指示灯灭）。

const WORKLET_CODE = `
class CaptureWorklet extends AudioWorkletProcessor {
  constructor() { super(); this._buf = []; this._n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      this._buf.push(ch.slice(0));
      this._n += ch.length;
      if (this._n >= 512) {              // 4 个渲染量子 @128 = 32ms @16k
        const out = new Float32Array(this._n);
        let off = 0;
        for (const b of this._buf) { out.set(b, off); off += b.length; }
        this.port.postMessage(out);
        this._buf = []; this._n = 0;
      }
    }
    return true;
  }
}
registerProcessor("capture-worklet", CaptureWorklet);
`;

let workletUrl: string | null = null;

interface SharedMic {
  sampleRate: number;
  unsub: (fn: (chunk: Float32Array) => void) => void;
}

let sharedMic: Promise<SharedMic> | null = null;
const subscribers = new Set<(chunk: Float32Array) => void>();

async function getSharedMic(): Promise<SharedMic> {
  if (sharedMic) return sharedMic;
  sharedMic = (async () => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    const ctx = new AudioContext({ sampleRate: 16000 });
    if (!workletUrl) {
      workletUrl = URL.createObjectURL(
        new Blob([WORKLET_CODE], { type: "application/javascript" }));
    }
    await ctx.audioWorklet.addModule(workletUrl);
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "capture-worklet");
    node.port.onmessage = (e) => {
      const chunk = new Float32Array(e.data);
      subscribers.forEach((fn) => fn(chunk));
    };
    src.connect(node);
    // worklet 必须连到 destination 才保证回调运行；gain=0 静音，不外放
    const sink = ctx.createGain();
    sink.gain.value = 0;
    node.connect(sink);
    sink.connect(ctx.destination);
    const mic: SharedMic = {
      sampleRate: ctx.sampleRate,
      unsub: (fn) => {
        subscribers.delete(fn);
        if (subscribers.size === 0) {
          // 无人订阅：释放麦克风，下次订阅重开
          try {
            node.disconnect();
            src.disconnect();
            sink.disconnect();
            stream.getTracks().forEach((t) => t.stop());
            ctx.close();
          } catch {
            // 资源可能已被浏览器回收
          }
          sharedMic = null;
        }
      },
    };
    return mic;
  })();
  return sharedMic;
}

async function subscribeMic(fn: (chunk: Float32Array) => void): Promise<Unsub> {
  const mic = await getSharedMic();
  subscribers.add(fn);
  return () => mic.unsub(fn);
}
type Unsub = () => void;

// ── 按住说话（微信对讲式） ────────────────────────────────────────────

export interface Recording {
  stop: () => Promise<Blob>;
}

export async function startRecording(): Promise<Recording> {
  const chunks: Float32Array[] = [];
  let rate = 16000;
  const unsub = await subscribeMic((c) => chunks.push(c));
  const mic = await getSharedMic();
  rate = mic.sampleRate;
  return {
    stop: () => {
      unsub();
      return Promise.resolve(encodeWav16(chunks, rate));
    },
  };
}

// ── 免提（对空气说话）：常开 VAD ─────────────────────────────────────

export interface HandsFreeOptions {
  /** 判句完成（一条完整话语，WAV 16k） */
  onSegment: (blob: Blob) => void;
  /** 声学打断触发（调用方负责停播报 + 打断当前轮） */
  onBargeIn: () => void;
  /** 当前是否在播报（播报中武装打断：阈值抬高 + 需持续人声） */
  isPlayback: () => boolean;
  /** 麦克风/采集错误（调用方负责关掉免提并提示） */
  onError: (msg: string) => void;
}

const HANGOVER_MS = 700;        // 静音判句时长
const HANGOVER_PLAYING_MS = 800;// 播报中（用户插话）判句更宽容
const MIN_SPEECH_MS = 350;      // 过短话语（咳嗽/碰撞）丢弃
const MAX_SEGMENT_MS = 60000;   // 60s 强制切段（后端 transcribe 上限同源）
const BARGE_IN_MS = 240;        // 播报中持续人声判打断
const BARGE_DISCARD_MS = 250;   // 打断瞬间截掉编码头（回声尾巴）
const START_SPEECH_MS = 60;     // 连续能量 60ms = 开口

export class HandsFree {
  private unsub: Unsub | null = null;
  private rate = 16000;
  private chunkMs = 32;
  private chunks: Float32Array[] = [];
  private speaking = false;
  private speechMs = 0;        // 开口判定累计
  private speechTotalMs = 0;   // 本段总人声时长
  private silenceMs = 0;
  private noiseFloor = 0.004;
  private bargeInDone = false;
  private discardMs = 0;

  async start(opts: HandsFreeOptions): Promise<void> {
    if (this.unsub) return;
    try {
      this.unsub = await subscribeMic((chunk) => this.onChunk(chunk, opts));
      const mic = await getSharedMic();
      this.rate = mic.sampleRate;
    } catch (e) {
      this.unsub = null;
      opts.onError(e instanceof Error ? e.message : String(e));
    }
  }

  stop(): void {
    if (this.unsub) {
      this.unsub();
      this.unsub = null;
    }
    this.reset();
  }

  private reset() {
    this.chunks = [];
    this.speaking = false;
    this.speechMs = 0;
    this.speechTotalMs = 0;
    this.silenceMs = 0;
    this.bargeInDone = false;
    this.discardMs = 0;
  }

  private onChunk(chunk: Float32Array, o: HandsFreeOptions) {
    this.chunkMs = (chunk.length / this.rate) * 1000;
    let sum = 0;
    for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
    const rms = Math.sqrt(sum / Math.max(1, chunk.length));
    const playing = o.isPlayback();
    // 静噪地板缓慢自适应；播报中阈值抬高 1.8x（AEC 残余回声不触发）
    const base = Math.max(this.noiseFloor * 3.5, 0.012);
    const thresh = playing ? base * 1.8 : base;

    if (!this.speaking) {
      this.noiseFloor = Math.min(0.02, this.noiseFloor * 0.98 + rms * 0.02);
      if (rms > thresh) {
        this.speechMs += this.chunkMs;
        this.chunks.push(chunk);
        if (this.speechMs >= START_SPEECH_MS) {
          this.speaking = true;
          this.speechTotalMs = this.speechMs;
        }
      } else {
        this.speechMs = Math.max(0, this.speechMs - this.chunkMs);
        if (this.speechMs === 0) this.chunks.length = 0;
      }
    } else {
      this.chunks.push(chunk);
      this.speechTotalMs += this.chunkMs;
    }

    // 声学打断：播报中累计人声 ≥240ms（开口前或开口后都算），一次触发
    if (playing && !this.bargeInDone) {
      const voiced = this.speaking ? this.speechTotalMs : this.speechMs;
      if (voiced >= BARGE_IN_MS) {
        this.bargeInDone = true;
        this.discardMs = BARGE_DISCARD_MS;
        o.onBargeIn();
      }
    }

    if (!this.speaking) return;

    if (rms <= thresh) this.silenceMs += this.chunkMs;
    else this.silenceMs = 0;
    const hangover = playing ? HANGOVER_PLAYING_MS : HANGOVER_MS;
    if (this.speechTotalMs >= MAX_SEGMENT_MS || this.silenceMs >= hangover) {
      const seg = {
        chunks: this.chunks,
        total: this.speechTotalMs,
        discard: this.discardMs,
      };
      this.reset();
      if (seg.total < MIN_SPEECH_MS) return;
      this.emit(seg, o);
    }
  }

  private emit(
    seg: { chunks: Float32Array[]; total: number; discard: number },
    o: HandsFreeOptions,
  ) {
    let chunks = seg.chunks;
    if (seg.discard > 0) {
      let cut = Math.floor((this.rate * seg.discard) / 1000);
      const kept: Float32Array[] = [];
      for (const c of chunks) {
        if (cut <= 0) {
          kept.push(c);
        } else if (c.length <= cut) {
          cut -= c.length;
        } else {
          kept.push(c.subarray(cut));
          cut = 0;
        }
      }
      chunks = kept;
    }
    try {
      o.onSegment(encodeWav16(chunks, this.rate));
    } catch (e) {
      o.onError(e instanceof Error ? e.message : String(e));
    }
  }
}

function encodeWav16(chunks: Float32Array[], sampleRate: number): Blob {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const pcm = new Float32Array(total);
  let off = 0;
  for (const c of chunks) {
    pcm.set(c, off);
    off += c.length;
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const wstr = (o: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i));
  };
  wstr(0, "RIFF");
  v.setUint32(4, 36 + pcm.length * 2, true);
  wstr(8, "WAVE");
  wstr(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  wstr(36, "data");
  v.setUint32(40, pcm.length * 2, true);
  let o = 44;
  for (let i = 0; i < pcm.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}
