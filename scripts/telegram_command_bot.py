#!/usr/bin/env python3
import os, json, time, threading, urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
STATE = ROOT / "state" / "telegram_update_offset.json"
LOG = ROOT / "logs" / "telegram-command-bot.log"

def log(s):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {s}\n")

def load_env():
    data = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    data.update(os.environ)
    return data

ENV = load_env()
TOKEN = (ENV.get("TELEGRAM_BOT_TOKEN") or ENV.get("TELEGRAM_TOKEN")
         or ENV.get("BOT_TOKEN") or "").strip()
CHAT = (ENV.get("TELEGRAM_CHAT_ID") or ENV.get("CHAT_ID") or "").strip()
BASE = f"https://api.telegram.org/bot{TOKEN}/"

def api(method, data=None, timeout=35):
    raw = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(BASE + method, data=raw)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        result = json.loads(r.read().decode())
    if not result.get("ok"):
        raise RuntimeError(str(result))
    return result["result"]

def send(text):
    # OLYPHANTS_NEWS_FEATURE: menu Telegram
    keyboard = {
        "keyboard": [["📰 NEWS"]],
        "resize_keyboard": True,
        "is_persistent": True
    }
    api("sendMessage", {
        "chat_id": CHAT,
        "text": text[:4000],
        "reply_markup": json.dumps(keyboard, ensure_ascii=False)
    })


def make_news_report():
    import sys
    import json
    from time import monotonic

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from ai.news_rss import get_news_context
    from ai.gemini_client import ask_gemini

    started = monotonic()
    news = get_news_context(limit=8)
    log("NEWS RSS/cache selesai %.2f detik; cache=%s; jumlah=%s" % (
        monotonic() - started,
        news.get("cache"),
        news.get("jumlah_berita")
    ))

    if not news.get("berita"):
        raise RuntimeError("Tidak ada berita dari RSS maupun cache.")

    task = (
        "Buat laporan berita terbaru Olyphants dalam Bahasa Indonesia. "
        "Gunakan hanya data berita yang diberikan. Kelompokkan berita "
        "kripto dan ekonomi global. Jelaskan dampak potensial terhadap BTC, "
        "ETH, volatilitas, dan risiko pasar. Bedakan fakta dengan analisis. "
        "Cantumkan judul dan tautan sumber. Jangan mengarang harga atau "
        "kejadian. Kesimpulan hanya untuk pemantauan PAPER/DEMO, bukan "
        "instruksi transaksi. Jangan menyarankan aktivasi LIVE."
    )

    gemini_started = monotonic()
    report = ask_gemini(task, json.dumps(news, ensure_ascii=False))
    log("NEWS Gemini selesai %.2f detik" % (
        monotonic() - gemini_started
    ))
    return report


def send_document(path, caption):
    boundary = "----OlyphantsTelegramBoundary"
    fields = {"chat_id": CHAT, "caption": caption[:900]}
    body = bytearray()
    for k, v in fields.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{path.name}\"\r\nContent-Type: text/plain\r\n\r\n".encode())
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + "sendDocument", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read().decode())
    if not result.get("ok"):
        raise RuntimeError(str(result))

def read_file(rel, limit=12000):
    p = ROOT / rel
    if not p.is_file():
        return f"[Belum tersedia: {rel}]"
    try:
        return p.read_text(errors="replace")[-limit:]
    except Exception as e:
        return f"[Gagal membaca {rel}: {e}]"

def command_output(args):
    import subprocess
    try:
        p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=12)
        return (p.stdout + p.stderr).strip() or "(tidak ada keluaran)"
    except Exception as e:
        return f"(tidak dapat dijalankan: {e})"

def make_report(kind):
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    sections = [
        "LAPORAN OLYPHANTS - " + kind,
        "Waktu: " + now,
        "Mode keselamatan: DEMO/PAPER saja. Bot Telegram tidak memiliki fungsi transaksi.",
        "",
        "STATUS REPOSITORI",
        command_output(["git", "status", "--short", "--branch"]),
        "",
        "PROSES ROBOT (indikasi proses, bukan bukti posisi exchange)",
        command_output(["bash", "-lc", "pgrep -af 'btc_robot|python.*-m btc' || true"]),
        "",
        "INVENTARIS PENGUJIAN",
        read_file("docs/TEST_INVENTORY.md", 9000),
    ]
    if kind in ("AUDIT ROBOT", "CEK PERBAIKAN", "CEK KESIAPAN LIVE"):
        sections += ["", "CATATAN AUDIT / BATASAN", read_file("docs/SAFEGUARD_AUDIT.md", 7000)]
    if kind in ("LAPORAN KINERJA", "LAPORAN STATUS"):
        sections += ["", "LOG ROBOT TERAKHIR", read_file("paper_d1h4.log", 7000)]
    if kind == "CEK KESIAPAN LIVE":
        sections += [
            "", "KEPUTUSAN KESELAMATAN",
            "STATUS: BELUM DIOTORISASI UNTUK LIVE.",
            "Laporan ini tidak mengaktifkan LIVE. Verifikasi data pasar nyata, paper trading multi-hari, proteksi risiko, dan pengujian exchange harus diselesaikan terpisah."
        ]
    sections += [
        "", "PENTING",
        "Isi laporan dibatasi pada file dan proses yang dapat dibaca lokal.",
        "Tidak mengklaim koneksi exchange aktif atau posisi akun nyata tanpa bukti langsung."
    ]
    outdir = ROOT / "reports"
    outdir.mkdir(exist_ok=True)
    path = outdir / ("telegram_" + kind.lower().replace(" ", "_") + ".txt")
    path.write_text("\n".join(sections) + "\n")
    return path

COMMANDS = {
    "AUDIT ROBOT": "AUDIT ROBOT",
    "LAPORAN STATUS": "LAPORAN STATUS",
    "CEK PERBAIKAN": "CEK PERBAIKAN",
    "LAPORAN KINERJA": "LAPORAN KINERJA",
    "CEK KESIAPAN LIVE": "CEK KESIAPAN LIVE",
}


def _gemini_sessions_file():
    folder = ROOT / "state"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "gemini_chat_sessions.json"


def _gemini_load_sessions():
    path = _gemini_sessions_file()
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _gemini_save_sessions(sessions):
    path = _gemini_sessions_file()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(sessions, ensure_ascii=False))
    temp.replace(path)



JOB_LOCK = threading.Lock()
ACTIVE_JOBS = {"ANALISIS": False, "NEWS": False}


def start_background_job(kind, work):
    with JOB_LOCK:
        if ACTIVE_JOBS.get(kind, False):
            send("Pekerjaan " + kind + " masih berjalan. Tunggu sampai selesai.")
            log(kind + " ditolak: pekerjaan sebelumnya masih aktif")
            return
        ACTIVE_JOBS[kind] = True

    def runner():
        started = time.monotonic()
        log(kind + " worker dimulai")
        try:
            result = work()
            log(kind + " selesai %.2f detik; panjang hasil=%s" % (
                time.monotonic() - started, len(str(result))
            ))
            heading = (
                "HASIL ANALISIS GEMINI OLYPHANTS\n\n"
                if kind == "ANALISIS"
                else "LAPORAN BERITA OLYPHANTS\n\n"
            )
            send(heading + str(result)[:3500])
            log(kind + " hasil dikirim")
        except Exception as exc:
            log(kind + " gagal %.2f detik: %s" % (
                time.monotonic() - started, type(exc).__name__
            ))
            try:
                send(kind + " gagal: " + type(exc).__name__
                     + ". Robot PAPER tetap berjalan.")
            except Exception as send_exc:
                log(kind + " notifikasi gagal: " + type(send_exc).__name__)
        finally:
            with JOB_LOCK:
                ACTIVE_JOBS[kind] = False
            log(kind + " worker berakhir; durasi %.2f detik" % (
                time.monotonic() - started
            ))

    threading.Thread(
        target=runner,
        name="ollyphants-" + kind.lower(),
        daemon=True
    ).start()


def handle(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != CHAT:
        log("Perintah ditolak: chat tidak diizinkan")
        return

    raw_text = (msg.get("text") or "").strip()
    if not raw_text:
        return

    text = str(raw_text).strip().upper()
    # Normalisasi command Telegram: /STATUS@BotName menjadi STATUS
    if text.startswith("/"):
        text = text.split()[0].split("@", 1)[0].lstrip("/")
    text = text.strip()
    if text.startswith("/"):
        text = text.split("@", 1)[0][1:].strip()
        text = text.replace("_", " ").upper()

    # Bantuan dan tes bot lokal
    if text in ("PING", "START", "HELP"):
        send(
            "OLYPHANTS TELEGRAM AKTIF.\\n"
            "MENU PERINTAH:\\n"
            "/status - laporan status dari Termux\\n"
            "/laporan - laporan operasional Termux\\n"
            "/kinerja - laporan kinerja PAPER Termux\\n"
            "/audit - audit robot lokal\\n"
            "/analisis - analisis kondisi dan kinerja robot PAPER Gemini\\n"
            "GEMINI TES - tes koneksi Gemini\\n"
            "Chat biasa - DeepSeek jika konektornya tersedia.\\n"
            "Gemini tidak digunakan untuk chat biasa."
        )
        return

    if text == "GEMINI TES":
        try:
            import sys
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from ai.gemini_client import ask_gemini

            result = ask_gemini(
                "Balas tepat dengan: GEMINI OLYPHANTS TERHUBUNG"
            )
            send("TES GEMINI BERHASIL\\n" + str(result)[:1000])
            log("Tes koneksi Gemini berhasil")
        except Exception as exc:
            detail = str(exc)
            if "429" in detail or "quota" in detail.lower():
                reply = "KUOTA GEMINI HABIS (HTTP 429)."
            else:
                reply = "TES GEMINI GAGAL: " + type(exc).__name__
            send(reply + "\\nRobot PAPER tetap berjalan.")
            log("Tes Gemini gagal: " + type(exc).__name__)
        return

    # ANALISIS hanya membaca data PAPER dan berjalan di worker.
    if text in ("/ANALISIS", "ANALISIS", "GEMINI ANALISIS", "ANALISIS PAPER"):
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        try:
            from ai.gemini_report import analyze_paper
            send("Analisis PAPER dimulai di latar belakang. Telegram tetap aktif.")
            start_background_job("ANALISIS", analyze_paper)
        except Exception as exc:
            log("Persiapan ANALISIS gagal: " + type(exc).__name__)
            send("Persiapan ANALISIS gagal: " + type(exc).__name__)
        return

    # Jangan aktifkan lagi sesi percakapan Gemini bebas
    if text in ("GEMINI", "STOPGEMINI", "STOP GEMINI"):
        send(
            "Chat bebas Gemini dinonaktifkan.\\n"
            "Gunakan /analisis untuk analisis PAPER atau GEMINI TES "
            "untuk tes koneksi. Chat biasa tidak akan dikirim ke Gemini."
        )
        return

    # Alias menu laporan lokal
    aliases = {
        "STATUS": "LAPORAN STATUS",
        "LAPORAN": "LAPORAN STATUS",
        "KINERJA": "LAPORAN KINERJA",
        "AUDIT": "AUDIT ROBOT",
        "PERBAIKAN": "CEK PERBAIKAN",
        "KESIAPAN LIVE": "CEK KESIAPAN LIVE",
    }
    kind = aliases.get(text, text)

    # Laporan operasional berasal dari file dan data Termux
    if kind in COMMANDS:
        try:
            report_path = make_report(COMMANDS[kind])
            send_document(
                report_path,
                "Laporan " + COMMANDS[kind]
                + " Olyphants. Sumber: data lokal Termux."
            )
            log("Laporan lokal terkirim: " + COMMANDS[kind])
        except Exception as exc:
            send("Gagal membuat laporan lokal: " + type(exc).__name__)
            log("Laporan lokal gagal: " + type(exc).__name__)
        return

    # NEWS memakai satu-satunya sumber: ai.news_rss beserta cache-nya.
    if text in ("📰 NEWS", "NEWS", "BERITA"):
        send("NEWS dimulai di latar belakang. Telegram tetap aktif.")
        start_background_job("NEWS", make_news_report)
        return

    # Chat umum hanya mencoba DeepSeek, tidak pernah fallback ke Gemini
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        from ai.deepseek_client import ask_deepseek
        result = ask_deepseek(raw_text)

        answer = str(result).strip()
        if not answer:
            answer = "DeepSeek tidak memberikan jawaban."
        send(answer[:3800])
        log("Chat DeepSeek berhasil menjawab")

    except ModuleNotFoundError as exc:
        if exc.name == "ai.deepseek_client":
            send(
                "Chat AI umum belum tersedia: konektor DeepSeek belum dipasang.\\n"
                "Gemini tidak dipanggil. Gunakan /analisis untuk analisis "
                "PAPER atau perintah laporan lokal."
            )
            log("Chat umum ditolak: konektor DeepSeek belum tersedia")
        else:
            send("DeepSeek belum dapat dijalankan: modul pendukung tidak ditemukan.")
            log("Modul DeepSeek tidak ditemukan: " + type(exc).__name__)

    except Exception as exc:
        send(
            "DeepSeek gagal menjawab: " + type(exc).__name__
            + ". Gemini tidak digunakan sebagai fallback."
        )
        log("DeepSeek chat error: " + type(exc).__name__)

def main():
    if not TOKEN or not CHAT:
        raise SystemExit("Token atau Chat ID tidak ditemukan di .env. Bot tidak dijalankan.")
    # Pastikan token benar tanpa menampilkan token ke log.
    me = api("getMe")
    log("Receiver aktif untuk bot @" + me.get("username", "unknown"))
    send("Penerima perintah Olyphants aktif. Ketik PING untuk tes. Audit hanya dikirim atas permintaan.")
    offset = 0
    if STATE.exists():
        try:
            offset = int(json.loads(STATE.read_text()).get("offset", 0))
        except Exception:
            pass
    while True:
        try:
            updates = api("getUpdates", {"timeout": 25, "offset": offset, "allowed_updates": json.dumps(["message"])}, timeout=35)
            for update in updates:
                offset = update["update_id"] + 1
                STATE.write_text(json.dumps({"offset": offset}))
                if "message" in update:
                    try:
                        handle(update["message"])
                    except Exception as e:
                        log("Gagal memproses pesan: " + type(e).__name__)
                        try:
                            send("Perintah gagal diproses. Periksa log penerima Telegram di Termux.")
                        except Exception:
                            pass
        except Exception as e:
            log("Polling error: " + type(e).__name__)
            time.sleep(5)

if __name__ == "__main__":
    main()
