import atexit
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
import subprocess
from pathlib import Path

from flask import Flask, jsonify, request, send_file

# tkinter 在 PyInstaller 打包後有可能缺席；缺少時退回主控台輸入，
# 避免整個程式因為一個對話框而直接結束。
try:
    from tkinter import Button, Entry, Frame, Label, PhotoImage, StringVar, TclError, Tk, messagebox
    TK_AVAILABLE = True
except ImportError:  # pragma: no cover - 取決於打包環境
    Button = Entry = Frame = Label = PhotoImage = StringVar = Tk = messagebox = None
    TclError = Exception
    TK_AVAILABLE = False

from flask import Flask, jsonify, request, send_file

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None


FLASK_HOST = "127.0.0.1"
APP_VERSION = "2026.10.01-datajson-10"
SCHEMA_VERSION = 6
BACKUP_MAX_COUNT = 30
BACKUP_INTERVAL_SECONDS = 10 * 60
LOCK_ACQUIRE_RETRY_SECONDS = 8
LOCK_RETRY_INTERVAL_SECONDS = 0.25
FIRST_CONNECT_TIMEOUT_SECONDS = 30
HEARTBEAT_TIMEOUT_SECONDS = 20
MACHINE_NO_MAX_LENGTH = 32

if getattr(sys, "frozen", False):
    EXE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR))
else:
    EXE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = EXE_DIR

LOCK_FILE = EXE_DIR / "lockfile.lock"
SESSION_FILE = EXE_DIR / "session.json"
DATA_FILE = EXE_DIR / "data.json"
TEMPLATE_HTML_FILE = BUNDLE_DIR / "index.html"
BACKUP_DIR = EXE_DIR / "backup"

app = Flask(__name__, static_folder=None)
EXIT_FLAG = threading.Event()
last_heartbeat_time = time.time()
WAIT_FIRST_CONNECT = True
lock_file_handle = None
windows_mutex_handle = None
active_machine_no = ""
active_login_at = 0
MUTEX_NAME = "Local\\ProgrammeVersionTracker_" + hashlib.sha256(
    os.path.normcase(str(EXE_DIR)).encode("utf-8")
).hexdigest()
file_save_lock = threading.RLock()
backup_slot_lock = threading.Lock()
next_backup_slot = None
next_backup_at = None
workspace_ready = False

def now_ms():
    return int(time.time() * 1000)


def iso_timestamp(value_ms=None):
    value_ms = now_ms() if value_ms is None else value_ms
    return datetime.datetime.fromtimestamp(value_ms / 1000, datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def get_template_path():
    return TEMPLATE_HTML_FILE


def get_random_port():
    """強制向系統要求一個當下絕對可用的隨機 Port，避免 8080 衝突"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((FLASK_HOST, 0))
        return sock.getsockname()[1]


def launch_modern_browser(url):
    """
    強制尋找並使用現代瀏覽器 (Edge 或 Chrome) 開啟 URL，避免舊版 Windows 預設使用 IE。
    如果都找不到，才退回系統預設的 webbrowser。
    """
    if os.name == "nt":
        prog_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
        prog_files_x86 = os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA", "C:\\Users\\Default\\AppData\\Local")

        # 優先順序：Edge -> Chrome (64bit) -> Chrome (32bit) -> Chrome (User Data)
        browser_paths = [
            os.path.join(prog_files_x86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(prog_files, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(prog_files_x86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe")
        ]

        for path in browser_paths:
            if os.path.exists(path):
                try:
                    subprocess.Popen([path, url])
                    return
                except Exception as e:
                    print(f"[Browser Launch Error] {e}")
                    continue
                    
    # 如果不是 Windows 系統，或者真的找不到 Edge/Chrome，則使用系統預設
    webbrowser.open(url)


# ===================== Lock lifecycle =====================
def remove_lockfile_if_possible():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def acquire_windows_mutex():
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return None
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception as exc:
        print(f"[Mutex initialization failed] {exc}")
        return None


def close_windows_mutex(handle):
    if not handle:
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
    except Exception:
        pass

def acquire_network_file_lock():
    deadline = time.monotonic() + LOCK_ACQUIRE_RETRY_SECONDS
    while True:
        handle = None
        try:
            LOCK_FILE.touch(exist_ok=True)
            handle = open(LOCK_FILE, "r+", encoding="utf-8")
            if os.name == "nt":
                if not msvcrt:
                    raise OSError("Windows file locking module is unavailable")
                if LOCK_FILE.stat().st_size == 0:
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                if not fcntl:
                    raise OSError("File locking is unavailable")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 以 "r+" 開啟後寫入才會落在指定位置；先截斷避免殘留舊內容。
            handle.seek(0)
            handle.truncate(0)
            handle.write(f"{os.getpid()} ")
            handle.flush()
            os.fsync(handle.fileno())
            return handle
        except (IOError, OSError):
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(LOCK_RETRY_INTERVAL_SECONDS)


def release_network_file_lock(handle):
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt" and msvcrt:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (IOError, OSError):
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass

def acquire_system_lock():
    global lock_file_handle, windows_mutex_handle
    if lock_file_handle is not None:
        return True
    local_mutex = acquire_windows_mutex() if os.name == "nt" else None
    if os.name == "nt" and local_mutex is None:
        return False
    network_lock = acquire_network_file_lock()
    if network_lock is None:
        close_windows_mutex(local_mutex)
        return False
    windows_mutex_handle = local_mutex
    lock_file_handle = network_lock
    return True


def release_system_lock():
    global lock_file_handle, windows_mutex_handle
    network_lock, local_mutex = lock_file_handle, windows_mutex_handle
    lock_file_handle, windows_mutex_handle = None, None
    release_network_file_lock(network_lock)
    close_windows_mutex(local_mutex)
    # 只有真正持有鎖的行程才可以刪除鎖檔；被擋在外的行程若誤刪，
    # 會讓第三人重新建立鎖檔並取得寫入權，造成資料互相覆蓋。
    if network_lock is not None:
        clear_session()
        remove_lockfile_if_possible()


atexit.register(release_system_lock)


# ===================== Session identity (Machine No) =====================
def clear_session():
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def read_session():
    """讀取目前佔用中的 Machine No；檔案不存在或毀損時回傳 None。"""
    try:
        with SESSION_FILE.open("r", encoding="utf-8") as file_handle:
            session = json.load(file_handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(session, dict):
        return None
    machine_no = str(session.get("machineNo", "")).strip()
    if not machine_no:
        return None
    try:
        login_at = int(session.get("loginAt", 0) or 0)
    except (TypeError, ValueError):
        login_at = 0
    return {
        "machineNo": machine_no,
        "host": str(session.get("host", "")).strip(),
        "loginAt": login_at,
    }


def write_session(machine_no):
    global active_machine_no, active_login_at
    active_login_at = now_ms()
    active_machine_no = machine_no
    write_json_atomic(
        SESSION_FILE,
        {
            "machineNo": machine_no,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "loginAt": active_login_at,
        },
    )


# ===================== Data validation =====================
def get_revision(data):
    try:
        return max(0, int(data.get("revision", 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def has_meaningful_user_data(data):
    if not isinstance(data, dict):
        return False
    if str(data.get("programNo", "")).strip() or str(data.get("programName", "")).strip():
        return True
    for records in (data.get("episodes") or {}).values():
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict) and any(str(record.get(field, "")).strip() for field in ("version", "fileNum", "staff", "date", "status", "condition")):
                return True
    return False


def validate_program_data(data):
    if not isinstance(data, dict):
        return False, "Data must be a JSON object."
    for field in ("id", "programNo", "programName"):
        if not isinstance(data.get(field), str):
            return False, f"Missing or invalid field: {field}."
    headers = data.get("headers")
    if not isinstance(headers, dict) or any(not isinstance(headers.get(key), str) for key in ("b", "c", "d", "e")):
        return False, "Column header data is incomplete."
    tracks = data.get("versionTracks")
    if not isinstance(tracks, list) or not tracks:
        return False, "Version track data is incomplete."
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("id"), str) or not track["id"]:
            return False, "Invalid version track ID."
        try:
            start, end = int(track.get("startEpisode")), int(track.get("endEpisode"))
        except (TypeError, ValueError):
            return False, "Invalid episode range."
        if start < 1 or end < start:
            return False, "Invalid episode range."
    
    episodes = data.get("episodes")
    if not isinstance(episodes, dict):
        return False, "Episode data is invalid."
    seen_file_numbers = {}
    for episode, records in episodes.items():
        episode_label = str(episode).strip()
        if not episode_label or not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            return False, "Episode record data is invalid."
        for record in records:
            file_number = str(record.get("fileNum", "")).strip()
            if not file_number:
                continue
            previous_episode = seen_file_numbers.get(file_number)
            if previous_episode is not None and previous_episode != episode_label:
                return False, (
                    f"Digital File Number {file_number} already belongs to Episode {previous_episode}; "
                    "the same number may only be used within one Episode."
                )
            seen_file_numbers[file_number] = episode_label
    revision = data.get("revision", 0)
    if not isinstance(revision, int) or revision < 0:
        return False, "Revision must be a non-negative integer."
    return True, ""

def default_program_data():
    return {
        "id": f"prog_{uuid.uuid4()}",
        "programNo": "",
        "programName": "",
        "versionTracks": [{"id": "track_std", "name": "", "startEpisode": 1, "endEpisode": 25}],
        "schemaVersion": SCHEMA_VERSION,
        "revision": 0,
        "lastModified": 0,
        "headers": {"b": "Digital File Number", "c": "Staff no", "d": "Update", "e": "Status"},
        "episodes": {},
    }


def read_data_file():
    try:
        with DATA_FILE.open("r", encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"[data.json read failed] {exc}")
        return None


def write_json_atomic(path, data):
    temporary_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as file_handle:
            json.dump(data, file_handle, ensure_ascii=False, indent=2)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

def initialize_workspace():
    global workspace_ready
    if workspace_ready:
        return
    with file_save_lock:
        if workspace_ready:
            return
        if not DATA_FILE.exists():
            write_json_atomic(DATA_FILE, default_program_data())
        else:
            data = read_data_file()
            if data is None:
                raise ValueError("data.json cannot be read; restore a backup before continuing.")
            valid, message = validate_program_data(data)
            if not valid:
                raise ValueError(f"data.json is invalid: {message}")
        workspace_ready = True


# ===================== Backups and restore =====================
def get_next_backup_slot():
    global next_backup_slot
    if next_backup_slot is not None:
        return next_backup_slot
    BACKUP_DIR.mkdir(exist_ok=True)
    latest_slot, latest_mtime = None, -1.0
    for backup_path in BACKUP_DIR.glob("backup_*.json"):
        match = re.fullmatch(r"backup_(\d{2})\.json", backup_path.name)
        if not match:
            continue
        slot = int(match.group(1))
        if not 1 <= slot <= BACKUP_MAX_COUNT:
            continue
        try:
            modified = backup_path.stat().st_mtime
        except OSError:
            continue
        if modified > latest_mtime:
            latest_slot, latest_mtime = slot, modified
    next_backup_slot = 1 if latest_slot is None else (latest_slot % BACKUP_MAX_COUNT) + 1
    return next_backup_slot


def create_cyclic_backup(reason="scheduled"):
    global next_backup_slot, next_backup_at
    with file_save_lock, backup_slot_lock:
        if not DATA_FILE.exists() or DATA_FILE.stat().st_size == 0:
            return None
        slot = get_next_backup_slot()
        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f"backup_{slot:02d}.json"
        temp_path = backup_path.with_name(f"{backup_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(DATA_FILE, temp_path)
            os.replace(temp_path, backup_path)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            print(f"[{reason} backup failed] {exc}")
            return None
        next_backup_slot = (slot % BACKUP_MAX_COUNT) + 1
        next_backup_at = now_ms() + BACKUP_INTERVAL_SECONDS * 1000
        return {"slot": slot, "file": backup_path.name, "createdAt": now_ms(), "reason": reason}

def list_backups():
    result = []
    if not BACKUP_DIR.is_dir():
        return result
    for backup_path in BACKUP_DIR.glob("backup_*.json"):
        match = re.fullmatch(r"backup_(\d{2})\.json", backup_path.name)
        if not match:
            continue
        try:
            stat = backup_path.stat()
            result.append({"slot": int(match.group(1)), "file": backup_path.name, "modifiedAt": int(stat.st_mtime * 1000), "size": stat.st_size})
        except OSError:
            pass
    return sorted(result, key=lambda item: item["slot"])


def restore_backup(slot):
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        raise ValueError("Invalid backup slot.")
    if not 1 <= slot <= BACKUP_MAX_COUNT:
        raise ValueError("Backup slot is out of range.")
    source = BACKUP_DIR / f"backup_{slot:02d}.json"
    if not source.exists():
        raise FileNotFoundError("Selected backup does not exist.")
    with file_save_lock:
        with source.open("r", encoding="utf-8") as file_handle:
            restored = json.load(file_handle)
        valid, message = validate_program_data(restored)
        if not valid:
            raise ValueError(f"Selected backup is invalid: {message}")
        # Preserve a recovery point before replacing current data.
        pre_restore = create_cyclic_backup(reason="pre_restore")
        old_data = read_data_file() or {}
        restored["revision"] = get_revision(old_data) + 1
        restored["schemaVersion"] = SCHEMA_VERSION
        restored["lastModified"] = now_ms()
        write_json_atomic(DATA_FILE, restored)
        return restored


def backup_scheduler():
    while not EXIT_FLAG.wait(BACKUP_INTERVAL_SECONDS):
        create_cyclic_backup(reason="scheduled")


# ===================== Server lifecycle =====================
def heartbeat_monitor():
    global WAIT_FIRST_CONNECT
    start_up_time = time.time()
    while not EXIT_FLAG.is_set():
        time.sleep(1)
        now = time.time()
        if WAIT_FIRST_CONNECT:
            if now - last_heartbeat_time < 8:
                WAIT_FIRST_CONNECT = False
            elif now - start_up_time >= FIRST_CONNECT_TIMEOUT_SECONDS:
                clean_and_exit()
        elif now - last_heartbeat_time > HEARTBEAT_TIMEOUT_SECONDS:
            clean_and_exit()


def clean_and_exit():
    if EXIT_FLAG.is_set():
        return
    EXIT_FLAG.set()
    release_system_lock()

    def force_quit():
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=force_quit, daemon=True).start()


def popup_msg(title, message, err=False):
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if err:
        messagebox.showerror(title, message)
    else:
        messagebox.showinfo(title, message)
    root.destroy()


# ===================== Tk dialog theming =====================
# 色碼取自 index.html 的 CSS 變數，讓 tkinter 視窗與程式主畫面一致。
THEME_SURFACE = "#132138"           # .modal-box
THEME_SURFACE_DEEP = "#0f1c30"      # .form-input / --row-dark
THEME_BORDER = "#2c3e5f"            # --border-color
THEME_FOCUS = "#3b82f6"
THEME_TEXT_BRIGHT = "#eef2f9"
THEME_TEXT = "#dbe4f0"
THEME_TEXT_MUTED = "#93a5c4"
THEME_TEXT_ON_ACCENT = "#ffffff"
THEME_ACCENT = "#60a5fa"            # --status-last-user
THEME_ERROR = "#f0473f"             # --warn-color
THEME_BTN_BLUE = "#2563eb"          # .btn-primary
THEME_BTN_BLUE_HOVER = "#1d4ed8"
THEME_BTN_GRAY = "#1b2a45"          # .btn-secondary
THEME_BTN_GRAY_HOVER = "#24365a"
THEME_BTN_GRAY_BORDER = "#31456b"
THEME_BTN_GRAY_TEXT = "#c9d6ea"


def build_app_icon():
    """建立與程式 favicon 同風格的小圖示（Tk PhotoImage 不支援 alpha，故用方塊底）。"""
    size = 32
    try:
        icon = PhotoImage(width=size, height=size)
        icon.put(THEME_BORDER, to=(0, 0, size, size))
        icon.put(THEME_SURFACE_DEEP, to=(1, 1, size - 1, size - 1))
        icon.put(THEME_TEXT, to=(10, 5, 22, 27))          # 文件主體
        icon.put(THEME_SURFACE_DEEP, to=(22, 5, 25, 8))   # 右上摺角
        icon.put(THEME_SURFACE_DEEP, to=(13, 14, 21, 16))
        icon.put(THEME_SURFACE_DEEP, to=(13, 19, 19, 21))
        return icon
    except Exception:
        return None


def apply_window_chrome(root, title):
    """套用與主畫面一致的深色底與標題列圖示。"""
    root.title(title)
    root.configure(bg=THEME_SURFACE)
    root.attributes("-topmost", True)
    icon = build_app_icon()
    if icon is not None:
        try:
            root.iconphoto(True, icon)
        except Exception:
            pass


def center_window(root, height_divisor=3):
    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2)
    y = max(0, (root.winfo_screenheight() - root.winfo_reqheight()) // height_divisor)
    root.geometry(f"+{x}+{y}")
    root.deiconify()


def make_button(parent, text, command, base, hover, foreground, border=None):
    """建立扁平化深色按鈕；Tk 9 不接受兩元素間距，故只用單一數值。"""
    options = {
        "text": text,
        "command": command,
        "bg": base,
        "fg": foreground,
        "activebackground": hover,
        "activeforeground": foreground,
        "relief": "flat",
        "bd": 0,
        "font": ("Segoe UI", 10, "bold"),
        "padx": 18,
        "pady": 8,
        "highlightthickness": 1 if border else 0,
    }
    if border:
        options["highlightbackground"] = border
        options["highlightcolor"] = border
    return Button(parent, **options)


def show_themed_dialog(title, lines, accent=THEME_ERROR):
    """以程式主題樣式顯示訊息，取代系統 messagebox；沒有 tkinter 時退回主控台。"""
    if not TK_AVAILABLE:
        print(f"[{title}]")
        for line in lines:
            print(line)
        return

    root = Tk()
    root.withdraw()
    root.resizable(False, False)
    apply_window_chrome(root, title)

    frame = Frame(root, bg=THEME_SURFACE, padx=26, pady=22)
    frame.pack()
    frame.columnconfigure(0, weight=1)

    Label(
        frame, text=title, bg=THEME_SURFACE, fg=accent,
        font=("Segoe UI", 15, "bold"), justify="left"
    ).grid(row=0, column=0, sticky="w")

    for index, line in enumerate(lines):
        Label(
            frame, text=line, bg=THEME_SURFACE, fg=THEME_TEXT,
            font=("Segoe UI", 10), justify="left"
        ).grid(row=index + 1, column=0, sticky="w", pady=2)

    make_button(
        frame, "Close", root.destroy,
        THEME_BTN_BLUE, THEME_BTN_BLUE_HOVER, THEME_TEXT_ON_ACCENT,
    ).grid(row=len(lines) + 1, column=0, sticky="e", pady=18)

    root.bind("<Return>", lambda event: root.destroy())
    root.bind("<Escape>", lambda event: root.destroy())
    center_window(root)
    root.grab_set()
    root.mainloop()


def console_prompt_machine_no():
    """打包後若無 tkinter，改用主控台輸入，避免整個程式無法啟動。"""
    print("=" * 58)
    print("Machine No is required to open this programme.")
    print("=" * 58)
    try:
        entered = input("Machine No: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    machine_no = normalize_machine_no(entered)
    if machine_no is None:
        print("Invalid Machine No.")
    return machine_no


def normalize_machine_no(value):
    """回傳整理後的 Machine No；若無效則回傳 None。"""
    machine_no = str(value or "").strip()
    if not machine_no:
        return None
    if len(machine_no) > MACHINE_NO_MAX_LENGTH:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in machine_no):
        return None
    return machine_no


def prompt_machine_no():
    """Open the login dialog so the operator identifies this machine before use.
    Returns the Machine No, or None when cancelled / window closed.

    Layout note: always grid, and padx/pady must be single values. The Tcl/Tk 9.0
    shipped with Python 3.14 rejects two-element screen distances (both tuple and
    list) with TclError: expected screen distance.
    """
    if not TK_AVAILABLE:
        return console_prompt_machine_no()

    result = {"machineNo": None}
    root = Tk()
    root.withdraw()
    root.resizable(False, False)
    apply_window_chrome(root, "Machine No")

    frame = Frame(root, bg=THEME_SURFACE, padx=26, pady=22)
    frame.pack()
    frame.columnconfigure(0, weight=1)

    Label(
        frame, text="Machine No", bg=THEME_SURFACE, fg=THEME_TEXT_BRIGHT,
        font=("Segoe UI", 15, "bold"),
    ).grid(row=0, column=0, sticky="w")
    Label(
        frame,
        text="Enter this machine's Machine No to open the programme.",
        bg=THEME_SURFACE, fg=THEME_TEXT_MUTED, font=("Segoe UI", 10),
    ).grid(row=1, column=0, sticky="w", pady=6)

    variable = StringVar()
    entry = Entry(
        frame, textvariable=variable, font=("Segoe UI", 13),
        bg=THEME_SURFACE_DEEP, fg=THEME_TEXT, insertbackground=THEME_TEXT,
        relief="flat", bd=0, highlightthickness=2,
        highlightbackground=THEME_BORDER, highlightcolor=THEME_FOCUS,
    )
    entry.grid(row=2, column=0, sticky="ew", pady=8)

    error = Label(frame, text="", bg=THEME_SURFACE, fg=THEME_ERROR, font=("Segoe UI", 10))
    error.grid(row=3, column=0, sticky="w")

    def submit(event=None):
        machine_no = normalize_machine_no(variable.get())
        if machine_no is None:
            error.config(
                text=f"Machine No is required. Use 1-{MACHINE_NO_MAX_LENGTH} characters, no control characters."
            )
            return
        result["machineNo"] = machine_no
        root.destroy()

    def cancel(event=None):
        result["machineNo"] = None
        root.destroy()

    buttons = Frame(frame, bg=THEME_SURFACE)
    buttons.grid(row=4, column=0, sticky="e", pady=18)
    make_button(
        buttons, "Cancel", cancel,
        THEME_BTN_GRAY, THEME_BTN_GRAY_HOVER, THEME_BTN_GRAY_TEXT, border=THEME_BTN_GRAY_BORDER,
    ).pack(side="right", padx=8)
    make_button(
        buttons, "Enter", submit,
        THEME_BTN_BLUE, THEME_BTN_BLUE_HOVER, THEME_TEXT_ON_ACCENT,
    ).pack(side="right")

    entry.bind("<Return>", submit)
    entry.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)

    center_window(root)
    entry.focus_set()
    root.grab_set()
    root.mainloop()
    return result["machineNo"]


def launch_browser_when_ready(port, timeout_seconds=20):
    """等待本機伺服器開始監聽後再開啟瀏覽器，避免競態造成的錯誤頁。"""
    url = f"http://{FLASK_HOST}:{port}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if EXIT_FLAG.is_set():
            return
        try:
            with socket.create_connection((FLASK_HOST, port), timeout=0.5):
                launch_modern_browser(url)
                return
        except OSError:
            time.sleep(0.2)
    print("[Browser Launch] server did not start listening in time; skipping browser launch")


def format_session_time(timestamp_ms):
    """把登入時間顯示成與程式主畫面一致的簡短格式。"""
    if not timestamp_ms:
        return ""
    try:
        moment = datetime.datetime.fromtimestamp(int(timestamp_ms) / 1000).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


def show_lock_conflict_popup():
    """Report who currently holds the lock, so the second user knows whom to contact."""
    session = read_session()
    if session:
        body = [f"Machine No {session['machineNo']} is using this programme."]
        logged_in_at = format_session_time(session.get("loginAt"))
        if logged_in_at:
            body.append(f"Logged in at {logged_in_at}")
    else:
        body = ["This programme is already open on another machine."]

    body += [
        "",
        "Please contact that machine to check whether they forgot to log out,",
        "or are still entering data.",
        "",
        "If they have left, ask them to run this programme again to release the lock.",
    ]
    show_themed_dialog("Programme In Use", body)


# ===================== API routes =====================
@app.route("/")
def index():
    initialize_workspace()
    if not TEMPLATE_HTML_FILE.exists():
        return "Bundled index.html template is missing.", 500
    return send_file(TEMPLATE_HTML_FILE)


@app.route("/api/load", methods=["GET"])
def load_data():
    try:
        initialize_workspace()
        data = read_data_file()
        if data is None:
            return jsonify({"status": "error", "message": "data.json cannot be read. Restore a backup before continuing."}), 500
        return jsonify({"status": "success", "data": data, "revision": get_revision(data)})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/save", methods=["POST"])
def save_data():
    try:
        initialize_workspace()
        incoming = request.get_json(silent=True)
        valid, message = validate_program_data(incoming)
        if not valid:
            return jsonify({"status": "error", "message": message}), 400
        force_overwrite = request.args.get("force") == "1"
        with file_save_lock:
            current = read_data_file()
            if current is None:
                return jsonify({"status": "error", "message": "Current data.json cannot be read; restore a backup first."}), 500
            current_revision = get_revision(current)
            
            if has_meaningful_user_data(current) and not has_meaningful_user_data(incoming):
                return jsonify({"status": "error", "message": "Blank data cannot overwrite an existing programme."}), 422
            if not force_overwrite and get_revision(incoming) != current_revision:
                return jsonify({"status": "conflict", "message": "Data has a newer version. Reload before saving again.", "serverRevision": current_revision}), 409
            if force_overwrite:
                incoming["id"] = current.get("id", incoming["id"])
            incoming["schemaVersion"] = SCHEMA_VERSION
            incoming["revision"] = current_revision + 1
            incoming["lastModified"] = now_ms()
            write_json_atomic(DATA_FILE, incoming)
        return jsonify({"status": "success", "revision": incoming["revision"], "lastModified": incoming["lastModified"]})
    except Exception as exc:
        print(f"[Save failed] {exc}")
        return jsonify({"status": "error", "message": "Unable to write data.json."}), 500

@app.route("/api/status", methods=["GET"])
def status():
    initialize_workspace()
    data = read_data_file() or {}
    return jsonify({
        "status": "success",
        "appVersion": APP_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "workingFile": str(DATA_FILE),
        "workingFileName": DATA_FILE.name,
        "machineNo": active_machine_no,
        "loginAt": active_login_at,
        "dataRevision": get_revision(data),
        "lastModified": data.get("lastModified", 0),
        "lastBackupAt": max((item["modifiedAt"] for item in list_backups()), default=0),
        "nextBackupAt": next_backup_at or (now_ms() + BACKUP_INTERVAL_SECONDS * 1000),
        "nextBackupSlot": get_next_backup_slot(),
        "backupCount": len(list_backups()),
    })


@app.route("/api/backups", methods=["GET"])
def backups():
    initialize_workspace()
    return jsonify({"status": "success", "backups": list_backups()})


@app.route("/api/backups/<int:slot>/restore", methods=["POST"])
def restore(slot):
    try:
        initialize_workspace()
        restored = restore_backup(slot)
        return jsonify({"status": "success", "data": restored, "revision": restored["revision"], "message": f"Restored backup_{slot:02d}.json"})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        print(f"[Restore failed] {exc}")
        return jsonify({"status": "error", "message": "Unable to restore selected backup."}), 500


@app.route("/ping", methods=["GET", "POST"])
def ping():
    global last_heartbeat_time, WAIT_FIRST_CONNECT
    last_heartbeat_time = time.time()
    WAIT_FIRST_CONNECT = False
    return "ok"


@app.route("/closeall", methods=["GET", "POST"])
def close_all():
    clean_and_exit()
    return "exit"

if __name__ == "__main__":
    try:
        if not acquire_system_lock():
            show_lock_conflict_popup()
            sys.exit(0)

        # 已取得鎖，才詢問身分；被擋的使用者不需要輸入 Machine No。
        entered_machine_no = prompt_machine_no()
        if entered_machine_no is None:
            sys.exit(0)

        write_session(entered_machine_no)

        initialize_workspace()
        create_cyclic_backup(reason="startup")
        
        # 使用隨機 Port
        CURRENT_PORT = get_random_port()
        
        threading.Thread(target=heartbeat_monitor, daemon=True).start()
        threading.Thread(target=backup_scheduler, daemon=True).start()

        # 等 Flask 真的開始監聽再開瀏覽器，否則瀏覽器可能先開而看到「無法連上」。
        threading.Thread(
            target=launch_browser_when_ready, args=(CURRENT_PORT,), daemon=True
        ).start()

        app.run(host=FLASK_HOST, port=CURRENT_PORT, debug=False, use_reloader=False)
    finally:
        release_system_lock()