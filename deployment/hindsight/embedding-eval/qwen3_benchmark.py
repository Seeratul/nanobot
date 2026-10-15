"""Synthetic bilingual retrieval and resource smoke test on the actual Pi."""

import argparse
import json
import math
import pathlib
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path("/tmp/nanobot-qwen3-eval")
URL = "http://127.0.0.1:8891"
ALIAS = "qwen3-embedding-0.6b-q8_0"
INSTRUCTION = "Instruct: Given a question about past conversations, retrieve the relevant memory that answers it.\nQuery: "
DOCS = [
    ("language", "Die Demo-Nutzerin Mira möchte Antworten auf Deutsch. Fachbegriffe dürfen Englisch bleiben. Sie bevorzugt kurze, direkte Erklärungen."),
    ("atlas-backend", "Im Projekt Atlas läuft das Backend mit Python 3.12 und PostgreSQL. Redis wurde wegen zusätzlicher Komplexität verworfen."),
    ("atlas-frontend", "The Atlas website uses TypeScript and Astro for its public frontend. This decision concerns the website, not the backend database."),
    ("repair-dns", "The intermittent connection failures were fixed by correcting the DNS server address. Restarting the application alone did not solve them."),
    ("repair-disk", "Der Startfehler wurde durch eine volle SSD verursacht. Nach dem Entfernen alter Build-Artefakte startete der Dienst wieder. DNS war bei diesem Vorfall korrekt."),
    ("vegan", "Mira eats vegan food and avoids dairy and eggs. When choosing a restaurant, plant-based main courses are required."),
    ("restaurant-other", "Jonas enjoys grilled fish and seafood. His preferences do not describe Mira's diet."),
    ("backup", "Für das Demo-Projekt wird jede Nacht ein verschlüsseltes Backup auf das NAS geschrieben. Ein Wiederherstellungstest ist monatlich geplant."),
    ("backup-other", "The public demo website is rebuilt from Git on demand. Its build cache is disposable and does not contain the database backup."),
    ("deployment", "Die Demo-App läuft auf einem Raspberry Pi 4 mit einer USB-SSD. Der Dienst lauscht ausschließlich auf Loopback und wird über einen SSH-Tunnel erreicht."),
    ("travel", "For the autumn trip, Mira chose a train to Vienna. She ruled out flying because she wants to avoid airport transfers."),
    ("travel-other", "For a separate work trip, Jonas booked a flight to Zurich. Mira is not joining that trip."),
    ("notifications", "Benachrichtigungen sollen nur bei erledigten Aufgaben, Fehlern oder nötigen Entscheidungen erscheinen. Wiederholte Meldungen ohne neue Information sind unerwünscht."),
    ("storage", "The proposed object store was rejected. Uploaded files stay on the local SSD, while their metadata is stored in PostgreSQL."),
    ("keyboard", "Mira verwendet eine deutsche QWERTZ-Tastatur. Tastenkombinationen müssen zum deutschen Layout passen."),
    ("monitor", "The display flicker disappeared after replacing the HDMI cable. Changing the keyboard layout had no effect on that fault."),
    ("budget", "Für den Demo-Shop wurde ein Hostingbudget von 20 Euro im Monat vereinbart. Das Limit gilt nicht für Miras privaten Urlaub."),
    ("garden", "Die Tomaten auf dem Balkon werden morgens gegossen. Diese Notiz gehört zum Gartenprojekt und betrifft keine Serverwartung."),
]
QUERIES = [
    ("What language should I use when answering Mira?", "language"),
    ("Which database and programming language did we choose for the Atlas backend?", "atlas-backend"),
    ("Mit welchem Framework wurde die öffentliche Atlas-Webseite gebaut?", "atlas-frontend"),
    ("Was hat die zeitweisen Verbindungsprobleme wirklich behoben?", "repair-dns"),
    ("Why did the service fail to start, and how was that fixed?", "repair-disk"),
    ("Welche Ernährungswünsche muss ein Restaurant für Mira erfüllen?", "vegan"),
    ("Where are the encrypted nightly backups kept?", "backup"),
    ("Which device hosts the demo app, and how can I access its local service?", "deployment"),
    ("Welches Verkehrsmittel hat Mira für die Wienreise gewählt?", "travel"),
    ("When should the assistant send notifications?", "notifications"),
    ("Wo speichern wir hochgeladene Dateien und deren Metadaten?", "storage"),
    ("What keyboard layout does Mira use?", "keyboard"),
    ("Wodurch wurde das Flackern des Bildschirms behoben?", "monitor"),
    ("What monthly hosting budget was agreed for the demo shop?", "budget"),
]


def request(path, payload=None):
    req = urllib.request.Request(URL + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as response:
        return json.load(response)


def meminfo():
    values = {}
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        key, rest = line.split(":", 1)
        if key in {"MemAvailable", "SwapFree"}:
            values[key] = int(rest.split()[0])
    return values


def status(pid):
    values = {}
    try:
        for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, rest = line.split(":", 1)
                values[key] = int(rest.split()[0])
    except FileNotFoundError:
        pass
    return values


def vector(text):
    start = time.monotonic()
    body = request("/v1/embeddings", {"model": ALIAS, "input": text, "encoding_format": "float"})
    elapsed = time.monotonic() - start
    data = body["data"][0]["embedding"]
    if len(data) != 1024 or not all(math.isfinite(x) for x in data):
        raise ValueError("invalid embedding dimensions or values")
    norm = math.sqrt(sum(x*x for x in data))
    if norm < 0.01:
        raise ValueError("empty embedding")
    return [x / norm for x in data], elapsed, body.get("usage", {})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--reuse-documents", action="store_true")
    args = parser.parse_args()
    baseline = meminfo()
    binary = ROOT / "build/bin/llama-server"
    server_log = ROOT / f"server-{args.threads}threads.log"
    samples = []
    stop = threading.Event()
    command = ["nice", "-n", "10", str(binary), "--model", str(ROOT / "Qwen3-Embedding-0.6B-Q8_0.gguf"),
               "--alias", ALIAS, "--host", "127.0.0.1", "--port", "8891", "--embedding",
               "--pooling", "last", "--threads", str(args.threads), "--threads-batch", str(args.threads),
               "--ctx-size", "2048", "--batch-size", "1024", "--ubatch-size", "1024", "--parallel", "1"]
    with server_log.open("w") as log:
        server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        def sample():
            while not stop.wait(0.25):
                point = {**meminfo(), **status(server.pid)}
                try:
                    point["temperature"] = int(pathlib.Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
                except OSError:
                    pass
                samples.append(point)
        monitor = threading.Thread(target=sample, daemon=True)
        monitor.start()
        try:
            started = time.monotonic()
            while True:
                if server.poll() is not None:
                    raise RuntimeError("embedding server exited: " + server_log.read_text()[-5000:])
                try:
                    if request("/health").get("status") == "ok":
                        break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() - started > 120:
                    raise TimeoutError("embedding server did not become ready")
                time.sleep(1)
            startup_seconds = time.monotonic() - started
            print(json.dumps({"stage": "server_ready", "threads": args.threads,
                              "startup_seconds": round(startup_seconds, 2)}), flush=True)
            vector("warm-up embedding")
            document_times = []
            cache = ROOT / "synthetic-document-vectors.json"
            if args.reuse_documents and cache.exists():
                docs = json.loads(cache.read_text())
            else:
                docs = []
                for doc_id, text in DOCS:
                    embedding, elapsed, usage = vector(text)
                    docs.append({"id": doc_id, "vector": embedding})
                    document_times.append(elapsed)
                    print(json.dumps({"stage": "document", "id": doc_id,
                                      "seconds": round(elapsed, 3), "usage": usage}), flush=True)
                cache.write_text(json.dumps(docs))
            query_times = []
            results = []
            for query, expected in QUERIES:
                embedding, elapsed, usage = vector(INSTRUCTION + query)
                ranked = sorted(((sum(a*b for a, b in zip(embedding, doc["vector"])), doc["id"])
                                 for doc in docs), reverse=True)
                query_times.append(elapsed)
                result = {"query": query, "expected": expected,
                          "top3": [{"id": doc_id, "score": round(score, 4)} for score, doc_id in ranked[:3]],
                          "seconds": round(elapsed, 3), "usage": usage}
                results.append(result)
                print(json.dumps({"stage": "query", **result}), flush=True)
            # Longer source chunks expose activation-memory and ingestion cost.
            long_chunk = "A technical project records decisions, failures and successful repairs. " * 28
            _, long_seconds, long_usage = vector(long_chunk)
            peak = max((p.get("VmHWM", p.get("VmRSS", 0)) for p in samples), default=0)
            report = {
                "model": "Qwen/Qwen3-Embedding-0.6B-GGUF", "quantization": "Q8_0",
                "dimensions": 1024, "threads": args.threads, "startup_seconds": startup_seconds,
                "synthetic_documents": len(docs), "synthetic_queries": len(results),
                "top1_hits": sum(r["top3"][0]["id"] == r["expected"] for r in results),
                "top3_hits": sum(any(x["id"] == r["expected"] for x in r["top3"]) for r in results),
                "query_median_seconds": statistics.median(query_times),
                "query_max_seconds": max(query_times),
                "document_median_seconds": statistics.median(document_times) if document_times else None,
                "long_chunk_seconds": long_seconds, "long_chunk_usage": long_usage,
                "server_peak_rss_mib": peak / 1024,
                "system_available_before_mib": baseline["MemAvailable"] / 1024,
                "system_available_min_mib": min(p["MemAvailable"] for p in samples) / 1024,
                "swap_used_delta_mib": (baseline["SwapFree"] - min(p["SwapFree"] for p in samples)) / 1024,
                "temperature_max_c": max(p.get("temperature", 0) for p in samples),
                "results": results,
                "limitations": "Small synthetic smoke test; no real conversations, production load, or concurrent Hindsight/PostgreSQL.",
            }
            output = ROOT / f"benchmark-{args.threads}threads.json"
            output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(json.dumps({"stage": "complete", **{k: v for k, v in report.items() if k != "results"}}), flush=True)
        finally:
            stop.set()
            monitor.join(timeout=2)
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
