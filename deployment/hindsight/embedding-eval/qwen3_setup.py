"""Isolated native llama.cpp build and verified official Qwen3 Q8 model."""

import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = pathlib.Path("/tmp/nanobot-qwen3-eval")
REVISION = "370f27d7550e0def9b39c1f16d3fbaa13aa67728"
MODEL_NAME = "Qwen3-Embedding-0.6B-Q8_0.gguf"
MODEL_SHA256 = "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
TAG = "v0.7.0"


def event(stage, **fields):
    print(json.dumps({"stage": stage, **fields}), flush=True)


def download(url, target):
    temp = target.with_suffix(target.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-qwen3-eval"})
    with urllib.request.urlopen(req, timeout=120) as response, temp.open("wb") as output:
        total = int(response.headers.get("Content-Length", 0))
        copied = 0
        reported = time.monotonic()
        while block := response.read(1024 * 1024):
            output.write(block)
            copied += len(block)
            if time.monotonic() - reported > 15:
                event("downloading", file=target.name, mib=round(copied / 1024**2),
                      total_mib=round(total / 1024**2))
                reported = time.monotonic()
    temp.replace(target)


ROOT.mkdir(exist_ok=True)
cmake = ROOT / "tools/cmake/data/bin/cmake"
if not cmake.exists():
    event("installing_isolated_cmake")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "--disable-pip-version-check", "--target", str(ROOT / "tools"), "cmake"], check=True)

source_archive = ROOT / "llama-source.tgz"
source = ROOT / "llama.cpp-0.7.0"
if not source.exists():
    event("downloading_pinned_source", tag=TAG)
    if not source_archive.exists():
        download(f"https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/tags/{TAG}", source_archive)
    with tarfile.open(source_archive) as archive:
        archive.extractall(ROOT, filter="data")

model = ROOT / MODEL_NAME
if not model.exists():
    event("downloading_official_model", revision=REVISION)
    download(f"https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/{REVISION}/{MODEL_NAME}", model)
with model.open("rb") as handle:
    actual = hashlib.file_digest(handle, "sha256").hexdigest()
if actual != MODEL_SHA256:
    raise RuntimeError("model checksum mismatch")
event("model_verified", bytes=model.stat().st_size, sha256=actual)

build = ROOT / "build"
event("configuring_native_build")
subprocess.run([str(cmake), "-S", str(source), "-B", str(build),
                "-DCMAKE_BUILD_TYPE=Release", "-DGGML_NATIVE=ON", "-DGGML_OPENMP=ON",
                "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF",
                "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_CURL=OFF", "-DLLAMA_OPENSSL=OFF"], check=True)
event("building_server", jobs=2)
with (ROOT / "build.log").open("w") as log:
    result = subprocess.run(["nice", "-n", "10", str(cmake), "--build", str(build),
                             "--target", "llama-server", "-j", "2"], stdout=log, stderr=subprocess.STDOUT)
if result.returncode:
    print((ROOT / "build.log").read_text()[-7000:])
    raise SystemExit(result.returncode)
event("build_complete", server=str(build / "bin/llama-server"))
