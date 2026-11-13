"""搭建嵌入 sidecar 独立环境（一次性；主 .venv 零改动）。

1. 在 models/embed-svc/venv 建独立虚拟环境；
2. 装 **CPU 版** torch（不碰主 .venv 的 cu128）+ sentence-transformers；
3. 下载 BAAI/bge-m3 快照 → model_path.txt 记录路径（支持 HF_ENDPOINT 镜像重跑）。

用法：python scripts/setup_embed_svc.py
网络：直连 HF 失败时设 HF_ENDPOINT=https://hf-mirror.com 重跑本脚本。
"""
import json
import os
import subprocess
import sys
import venv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVC_HOME = os.path.join(os.path.dirname(REPO), "models", "embed-svc")
if not os.path.isdir(os.path.dirname(SVC_HOME)):
    SVC_HOME = os.path.join(os.path.expanduser("~"), ".omniagent", "models", "embed-svc")
VENV_DIR = os.path.join(SVC_HOME, "venv")
MODEL_NAME = "BAAI/bge-m3"
MODEL_PATH_TXT = os.path.join(SVC_HOME, "model_path.txt")
if any(a.startswith("--model") for a in sys.argv):
    i = [n for n, a in enumerate(sys.argv) if a.startswith("--model")][0]
    MODEL_NAME = (sys.argv[i].split("=", 1)[1] if "=" in sys.argv[i]
                  else sys.argv[i + 1])


def _py() -> str:
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def run(cmd, **kw):
    print("+", " ".join(cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise SystemExit(f"失败: {' '.join(cmd)}")


def main() -> int:
    os.makedirs(SVC_HOME, exist_ok=True)
    print(f"sidecar home: {SVC_HOME}")
    if not os.path.isfile(_py()):
        run([sys.executable, "-m", "venv", VENV_DIR])
    run([_py(), "-m", "pip", "install", "--upgrade", "pip"])
    # CPU torch：显式用 pytorch CPU 源，避免拉进 2.5GB 的 CUDA 轮子（sidecar 不需要）
    try:
        run([_py(), "-m", "pip", "install", "torch",
             "--index-url", "https://download.pytorch.org/whl/cpu"])
    except SystemExit:
        print("CPU torch 源失败——回退默认源")
        run([_py(), "-m", "pip", "install", "torch"])
    run([_py(), "-m", "pip", "install", "sentence-transformers", "huggingface_hub"])

    dl = os.path.join(SVC_HOME, "dl_model.py")
    with open(dl, "w", encoding="utf-8") as f:
        f.write("from huggingface_hub import snapshot_download\n"
                "p = snapshot_download(" + repr(MODEL_NAME) + ")\n"
                "open(" + repr(MODEL_PATH_TXT) + ", 'w', encoding='utf-8').write(p)\n"
                "print('model at:', p)\n")
    run([_py(), dl])
    with open(os.path.join(SVC_HOME, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"model": MODEL_NAME,
                   "model_path": open(MODEL_PATH_TXT, encoding="utf-8").read().strip()
                   if os.path.exists(MODEL_PATH_TXT) else "",
                   "venv": VENV_DIR}, f, ensure_ascii=False, indent=1)
    print("setup 完成。模型路径见 model_path.txt；sidecar 由 embedder 自动拉起。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
